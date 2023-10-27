from __future__ import annotations

from typing import Any, Dict, Text, List, Optional

from jinja2 import Template
from structlog.contextvars import (
    bound_contextvars,
)
from rasa.core.policies.flows.flow_exceptions import (
    FlowCircuitBreakerTrippedException,
    FlowException,
)
from rasa.core.policies.flows.flow_step_result import (
    FlowActionPrediction,
    ContinueFlowWithNextStep,
    FlowStepResult,
    PauseFlowReturnPrediction,
)
from rasa.dialogue_understanding.stack.dialogue_stack import DialogueStack
from rasa.dialogue_understanding.stack.frames import (
    BaseFlowStackFrame,
    DialogueStackFrame,
    UserFlowStackFrame,
)
from rasa.dialogue_understanding.patterns.collect_information import (
    CollectInformationPatternFlowStackFrame,
)
from rasa.dialogue_understanding.patterns.completed import (
    CompletedPatternFlowStackFrame,
)
from rasa.dialogue_understanding.patterns.continue_interrupted import (
    ContinueInterruptedPatternFlowStackFrame,
)
from rasa.dialogue_understanding.stack.frames.flow_stack_frame import FlowStackFrameType
from rasa.dialogue_understanding.stack.utils import (
    top_user_flow_frame,
)

from pypred import Predicate

from rasa.shared.core.constants import (
    ACTION_LISTEN_NAME,
    ACTION_SEND_TEXT_NAME,
)
from rasa.shared.core.events import Event, SlotSet
from rasa.shared.core.flows.flow import (
    END_STEP,
    ActionFlowStep,
    BranchFlowStep,
    ContinueFlowStep,
    ElseFlowLink,
    EndFlowStep,
    Flow,
    FlowStep,
    FlowsList,
    GenerateResponseFlowStep,
    IfFlowLink,
    SlotRejection,
    LinkFlowStep,
    SetSlotsFlowStep,
    CollectInformationFlowStep,
    StaticFlowLink,
)
from rasa.shared.core.domain import Domain
from rasa.shared.core.trackers import (
    DialogueStateTracker,
)
import structlog

structlogger = structlog.get_logger()

MAX_NUMBER_OF_STEPS = 250


def render_template_variables(text: str, context: Dict[Text, Any]) -> str:
    """Replace context variables in a text."""
    return Template(text).render(context)


def is_condition_satisfied(
    predicate: Text, context: Dict[str, Any], tracker: DialogueStateTracker
) -> bool:
    """Evaluate a predicate condition."""

    # attach context to the predicate evaluation to allow conditions using it
    context = {"context": context}

    document: Dict[str, Any] = context.copy()
    document.update(tracker.current_slot_values())

    p = Predicate(render_template_variables(predicate, context))
    try:
        return p.evaluate(document)
    except (TypeError, Exception) as e:
        structlogger.error(
            "flow.predicate.error",
            predicate=predicate,
            document=document,
            error=str(e),
        )
        return False


def is_step_end_of_flow(step: FlowStep) -> bool:
    """Check if a step is the end of a flow."""
    return (
        step.id == END_STEP
        or
        # not quite at the end but almost, so we'll treat it as the end
        step.id == ContinueFlowStep.continue_step_for_id(END_STEP)
    )


def select_next_step_id(
    current: FlowStep,
    condition_evaluation_context: Dict[str, Any],
    tracker: DialogueStateTracker,
) -> Optional[Text]:
    """Selects the next step id based on the current step."""
    next = current.next
    if len(next.links) == 1 and isinstance(next.links[0], StaticFlowLink):
        return next.links[0].target

    # evaluate if conditions
    for link in next.links:
        if isinstance(link, IfFlowLink) and link.condition:
            if is_condition_satisfied(
                link.condition, condition_evaluation_context, tracker
            ):
                return link.target

    # evaluate else condition
    for link in next.links:
        if isinstance(link, ElseFlowLink):
            return link.target

    if next.links:
        structlogger.error(
            "flow.link.failed_to_select_branch",
            current=current,
            links=next.links,
            tracker=tracker,
        )
        return None
    if current.id == END_STEP:
        # we are already at the very end of the flow. There is no next step.
        return None
    elif isinstance(current, LinkFlowStep):
        # link steps don't have a next step, so we'll return the end step
        return END_STEP
    else:
        structlogger.error(
            "flow.step.failed_to_select_next_step",
            step=current,
            tracker=tracker,
        )
        return None


def select_next_step(
    current_step: FlowStep,
    current_flow: Flow,
    stack: DialogueStack,
    tracker: DialogueStateTracker,
) -> Optional[FlowStep]:
    """Get the next step to execute."""
    next_id = select_next_step_id(current_step, stack.current_context(), tracker)
    step = current_flow.step_by_id(next_id)
    structlogger.debug(
        "flow.step.next",
        next_id=step.id if step else None,
        current_id=current_step.id,
        flow_id=current_flow.id,
    )
    return step


def advance_top_flow_on_stack(updated_id: str, stack: DialogueStack) -> None:
    """Advance the top flow on the stack."""
    if (top := stack.top()) and isinstance(top, BaseFlowStackFrame):
        top.step_id = updated_id


def events_from_set_slots_step(step: SetSlotsFlowStep) -> List[Event]:
    """Create events from a set slots step."""
    return [SlotSet(slot["key"], slot["value"]) for slot in step.slots]


def events_for_collect_step(
    step: CollectInformationFlowStep, tracker: DialogueStateTracker
) -> List[Event]:
    """Create events for a collect step."""
    # reset the slot if its already filled and the collect information shouldn't
    # be skipped
    slot = tracker.slots.get(step.collect, None)

    if slot and slot.has_been_set and step.ask_before_filling:
        return [SlotSet(step.collect, slot.initial_value)]
    else:
        return []


def trigger_pattern_continue_interrupted(
    current_frame: DialogueStackFrame, stack: DialogueStack, flows: FlowsList
) -> None:
    """Trigger the pattern to continue an interrupted flow if needed."""
    # get previously started user flow that will be continued
    previous_user_flow_frame = top_user_flow_frame(stack)
    previous_user_flow_step = (
        previous_user_flow_frame.step(flows) if previous_user_flow_frame else None
    )
    previous_user_flow = (
        previous_user_flow_frame.flow(flows) if previous_user_flow_frame else None
    )

    if (
        isinstance(current_frame, UserFlowStackFrame)
        and previous_user_flow_step is not None
        and previous_user_flow is not None
        and current_frame.frame_type == FlowStackFrameType.INTERRUPT
        and not is_step_end_of_flow(previous_user_flow_step)
    ):
        stack.push(
            ContinueInterruptedPatternFlowStackFrame(
                previous_flow_name=previous_user_flow.readable_name(),
            )
        )


def trigger_pattern_completed(
    current_frame: DialogueStackFrame, stack: DialogueStack, flows: FlowsList
) -> None:
    """Trigger the pattern indicating that the stack is empty, if needed."""
    if not stack.is_empty() or not isinstance(current_frame, UserFlowStackFrame):
        return

    completed_flow = current_frame.flow(flows)
    completed_flow_name = completed_flow.readable_name() if completed_flow else None
    stack.push(
        CompletedPatternFlowStackFrame(
            previous_flow_name=completed_flow_name,
        )
    )


def trigger_pattern_ask_collect_information(
    collect: str,
    stack: DialogueStack,
    rejections: List[SlotRejection],
    utter: str,
) -> None:
    """Trigger the pattern to ask for a slot value."""
    stack.push(
        CollectInformationPatternFlowStackFrame(
            collect=collect,
            utter=utter,
            rejections=rejections,
        )
    )


def reset_scoped_slots(
    current_flow: Flow, tracker: DialogueStateTracker
) -> List[Event]:
    """Reset all scoped slots."""

    def _reset_slot(slot_name: Text, dialogue_tracker: DialogueStateTracker) -> None:
        slot = dialogue_tracker.slots.get(slot_name, None)
        initial_value = slot.initial_value if slot else None
        events.append(SlotSet(slot_name, initial_value))

    events: List[Event] = []

    not_resettable_slot_names = set()

    for step in current_flow.steps:
        if isinstance(step, CollectInformationFlowStep):
            # reset all slots scoped to the flow
            if step.reset_after_flow_ends:
                _reset_slot(step.collect, tracker)
            else:
                not_resettable_slot_names.add(step.collect)

    # slots set by the set slots step should be reset after the flow ends
    # unless they are also used in a collect step where `reset_after_flow_ends`
    # is set to `False`
    resettable_set_slots = [
        slot["key"]
        for step in current_flow.steps
        if isinstance(step, SetSlotsFlowStep)
        for slot in step.slots
        if slot["key"] not in not_resettable_slot_names
    ]

    for name in resettable_set_slots:
        _reset_slot(name, tracker)

    return events


def advance_flows(
    tracker: DialogueStateTracker, domain: Domain, flows: FlowsList
) -> FlowActionPrediction:
    """Advance the flows.

    Either start a new flow or advance the current flow.

    Args:
        tracker: The tracker to get the next action for.

    Returns:
    The predicted action and the events to run.
    """
    stack = DialogueStack.from_tracker(tracker)
    if stack.is_empty():
        # if there are no flows, there is nothing to do
        return FlowActionPrediction(None, 0.0)

    previous_stack = stack.as_dict()
    prediction = select_next_action(stack, tracker, domain, flows)
    if previous_stack != stack.as_dict():
        # we need to update dialogue stack to persist the state of the executor
        if not prediction.events:
            prediction.events = []
        prediction.events.append(stack.persist_as_event())
    return prediction


def select_next_action(
    stack: DialogueStack,
    tracker: DialogueStateTracker,
    domain: Domain,
    flows: FlowsList,
) -> FlowActionPrediction:
    """Select the next action to execute.

    Advances the current flow and returns the next action to execute. A flow
    is advanced until it is completed or until it predicts an action. If
    the flow is completed, the next flow is popped from the stack and
    advanced. If there are no more flows, the action listen is predicted.

    Args:
        tracker: The tracker to get the next action for.

    Returns:
        The next action to execute, the events that should be applied to the
    tracker and the confidence of the prediction.
    """
    step_result: FlowStepResult = ContinueFlowWithNextStep()

    tracker = tracker.copy()

    number_of_initial_events = len(tracker.events)

    number_of_steps_taken = 0

    while isinstance(step_result, ContinueFlowWithNextStep):

        number_of_steps_taken += 1
        if number_of_steps_taken > MAX_NUMBER_OF_STEPS:
            raise FlowCircuitBreakerTrippedException(stack, number_of_steps_taken)

        active_frame = stack.top()
        if not isinstance(active_frame, BaseFlowStackFrame):
            # If there is no current flow, we assume that all flows are done
            # and there is nothing to do. The assumption here is that every
            # flow ends with an action listen.
            step_result = PauseFlowReturnPrediction(
                FlowActionPrediction(ACTION_LISTEN_NAME, 1.0)
            )
            break

        with bound_contextvars(flow_id=active_frame.flow_id):
            structlogger.debug(
                "flow.execution.loop", previous_step_id=active_frame.step_id
            )
            current_flow = active_frame.flow(flows)
            current_step = select_next_step(
                active_frame.step(flows), current_flow, stack, tracker
            )

            if not current_step:
                continue

            advance_top_flow_on_stack(current_step.id, stack)

            with bound_contextvars(step_id=current_step.id):
                step_result = run_step(
                    current_step,
                    current_flow,
                    stack,
                    tracker,
                    domain.action_names_or_texts,
                    flows,
                )
                tracker.update_with_events(step_result.events)

    gathered_events = list(tracker.events)[number_of_initial_events:]
    if isinstance(step_result, PauseFlowReturnPrediction):
        prediction = step_result.action_prediction
        # make sure we really return all events that got created during the
        # step execution of all steps (not only the last one)
        prediction.events = gathered_events
        return prediction
    else:
        structlogger.warning("flow.step.execution.no_action")
        return FlowActionPrediction(None, 0.0)


def run_step(
    step: FlowStep,
    flow: Flow,
    stack: DialogueStack,
    tracker: DialogueStateTracker,
    available_actions: List[str],
    flows: FlowsList,
) -> FlowStepResult:
    """Run a single step of a flow.

    Returns the predicted action and a list of events that were generated
    during the step. The predicted action can be `None` if the step
    doesn't generate an action. The list of events can be empty if the
    step doesn't generate any events.

    Raises a `FlowException` if the step is invalid.

    Args:
        step: The step to run.
        flow: The flow that the step belongs to.
        stack: The stack that the flow is on.
        tracker: The tracker to run the step on.
        available_actions: The actions that are available in the domain.
        flows: All flows.

    Returns:
    A result of running the step describing where to transition to.
    """
    if isinstance(step, CollectInformationFlowStep):
        structlogger.debug("flow.step.run.collect")
        trigger_pattern_ask_collect_information(
            step.collect, stack, step.rejections, step.utter
        )

        events = events_for_collect_step(step, tracker)
        return ContinueFlowWithNextStep(events=events)

    elif isinstance(step, ActionFlowStep):
        if not step.action:
            raise FlowException(f"Action not specified for step {step}")

        context = {"context": stack.current_context()}
        action_name = render_template_variables(step.action, context)

        if action_name in available_actions:
            structlogger.debug("flow.step.run.action", context=context)
            return PauseFlowReturnPrediction(FlowActionPrediction(action_name, 1.0))
        else:
            structlogger.warning("flow.step.run.action.unknown", action=action_name)
            return ContinueFlowWithNextStep()

    elif isinstance(step, LinkFlowStep):
        structlogger.debug("flow.step.run.link")
        stack.push(
            UserFlowStackFrame(
                flow_id=step.link,
                frame_type=FlowStackFrameType.LINK,
            ),
            # push this below the current stack frame so that we can
            # complete the current flow first and then continue with the
            # linked flow
            index=-1,
        )
        return ContinueFlowWithNextStep()

    elif isinstance(step, SetSlotsFlowStep):
        structlogger.debug("flow.step.run.slot")
        events = events_from_set_slots_step(step)
        return ContinueFlowWithNextStep(events=events)

    elif isinstance(step, BranchFlowStep):
        structlogger.debug("flow.step.run.branch")
        return ContinueFlowWithNextStep()

    elif isinstance(step, GenerateResponseFlowStep):
        structlogger.debug("flow.step.run.generate_response")
        generated = step.generate(tracker)
        action_prediction = FlowActionPrediction(
            ACTION_SEND_TEXT_NAME,
            1.0,
            metadata={"message": {"text": generated}},
        )
        return PauseFlowReturnPrediction(action_prediction)

    elif isinstance(step, EndFlowStep):
        # this is the end of the flow, so we'll pop it from the stack
        structlogger.debug("flow.step.run.flow_end")
        current_frame = stack.pop()
        trigger_pattern_continue_interrupted(current_frame, stack, flows)
        trigger_pattern_completed(current_frame, stack, flows)
        reset_events = reset_scoped_slots(flow, tracker)
        return ContinueFlowWithNextStep(events=reset_events)

    else:
        raise FlowException(f"Unknown flow step type {type(step)}")
