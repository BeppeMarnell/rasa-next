from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Text, List, Optional, Tuple

from rasa.shared.core.constants import (
    FLOW_STACK_SLOT,
)
from rasa.shared.core.flows.flow import (
    START_STEP,
    Flow,
    FlowStep,
    FlowsList,
)
from rasa.shared.core.trackers import (
    DialogueStateTracker,
)
import structlog

structlogger = structlog.get_logger()


@dataclass
class FlowStack:
    """Represents the current flow stack."""

    frames: List[FlowStackFrame]

    @staticmethod
    def from_dict(data: List[Dict[Text, Any]]) -> FlowStack:
        """Creates a `FlowStack` from a dictionary.

        Args:
            data: The dictionary to create the `FlowStack` from.

        Returns:
            The created `FlowStack`.
        """
        return FlowStack([FlowStackFrame.from_dict(frame) for frame in data])

    def as_dict(self) -> List[Dict[Text, Any]]:
        """Returns the `FlowStack` as a dictionary.

        Returns:
            The `FlowStack` as a dictionary.
        """
        return [frame.as_dict() for frame in self.frames]

    def push(self, frame: FlowStackFrame, index: Optional[int] = None) -> None:
        """Pushes a new frame onto the stack.

        Args:
            frame: The frame to push onto the stack.
            index: The index to insert the frame at. If `None`, the frame
                is appended to the stack.
        """
        if index is None:
            self.frames.append(frame)
        else:
            self.frames.insert(index, frame)

    def update(self, frame: FlowStackFrame) -> None:
        """Updates the topmost frame.

        Args:
            frame: The frame to update.
        """
        if not self.is_empty():
            self.pop()

        self.push(frame)

    def advance_top_flow(self, updated_id: Text) -> None:
        """Updates the topmost flow step.

        Args:
            updated_id: The updated flow step ID.
        """
        if top := self.top():
            top.step_id = updated_id

    def pop(self) -> FlowStackFrame:
        """Pops the topmost frame from the stack.

        Returns:
            The popped frame.
        """
        return self.frames.pop()

    def top(self) -> Optional[FlowStackFrame]:
        """Returns the topmost frame from the stack.

        Returns:
            The topmost frame.
        """
        if self.is_empty():
            return None

        return self.frames[-1]

    def top_flow(self, flows: FlowsList) -> Optional[Flow]:
        """Returns the topmost flow from the stack.

        Args:
            flows: The flows to use.

        Returns:
            The topmost flow.
        """
        if not (top := self.top()):
            return None

        return flows.flow_by_id(top.flow_id)

    def topmost_user_frame(
        self, flows: FlowsList
    ) -> Tuple[Optional[FlowStep], Optional[Flow]]:
        """Returns the topmost user frame from the stack.

        Returns:
            The topmost user frame.
        """
        for frame in reversed(self.frames):
            if frame.frame_type in STACK_FRAME_TYPES_WITH_USER_FLOWS:
                flow = flows.flow_by_id(frame.flow_id)
                return flow.step_by_id(frame.step_id), flow

        return None, None

    def top_flow_step(self, flows: FlowsList) -> Optional[FlowStep]:
        """Get the current flow step.

        Returns:
        The current flow step or `None` if no flow is active.
        """
        if not (top := self.top()) or not (top_flow := self.top_flow(flows)):
            return None

        return top_flow.step_by_id(top.step_id)

    def is_empty(self) -> bool:
        """Checks if the stack is empty.

        Returns:
            `True` if the stack is empty, `False` otherwise.
        """
        return len(self.frames) == 0

    @staticmethod
    def from_tracker(tracker: DialogueStateTracker) -> FlowStack:
        """Creates a `FlowStack` from a tracker.

        Args:
            tracker: The tracker to create the `FlowStack` from.

        Returns:
            The created `FlowStack`.
        """
        flow_stack = tracker.get_slot(FLOW_STACK_SLOT) or []
        return FlowStack.from_dict(flow_stack)


class StackFrameType(str, Enum):
    INTERRUPT = "interrupt"
    """The frame is an interrupt frame.

    This means that the previous flow was interrupted by this flow. An
    interrupt should be used for frames that span multiple turns and
    where we expect the user needing help to get back to the previous
    flow."""
    REMARK = "remark"
    """The frame is a short remark - in a sense, a short interruption.

    This means that the previous flow was interrupted by this flow, but the
    interruption is short and the previous flow should be resumed right away
    after this flow is finished."""
    LINK = "link"
    """The frame is a link frame.

    This means that the previous flow linked to this flow."""
    CORRECTION = "correction"
    """The frame is a correction frame.

    This means that the previous flow was corrected by this flow."""
    REGULAR = "regular"
    """The frame is a regular frame.

    In all other cases, this is the case."""
    DOCSEARCH = "docsearch"
    """The frame to indicate the docsearch policy should act."""

    INTENTLESS = "intentless"
    """The frame to indicate the intentless policy should act."""

    @staticmethod
    def from_str(typ: Optional[Text]) -> "StackFrameType":
        """Creates a `StackFrameType` from a string."""
        if typ is None:
            return StackFrameType.REGULAR
        elif typ == StackFrameType.INTERRUPT.value:
            return StackFrameType.INTERRUPT
        elif typ == StackFrameType.LINK.value:
            return StackFrameType.LINK
        elif typ == StackFrameType.REGULAR.value:
            return StackFrameType.REGULAR
        elif typ == StackFrameType.CORRECTION.value:
            return StackFrameType.CORRECTION
        elif typ == StackFrameType.DOCSEARCH.value:
            return StackFrameType.DOCSEARCH
        elif typ == StackFrameType.INTENTLESS.value:
            return StackFrameType.INTENTLESS
        elif typ == StackFrameType.REMARK.value:
            return StackFrameType.REMARK
        else:
            raise NotImplementedError


# Types of frames which will be filled by user defined "normal" flows.
STACK_FRAME_TYPES_WITH_USER_FLOWS = {
    StackFrameType.INTERRUPT,
    StackFrameType.REGULAR,
    StackFrameType.LINK,
}


@dataclass
class FlowStackFrame:
    """Represents the current flow step."""

    flow_id: Text
    """The ID of the current flow."""
    step_id: Text = START_STEP
    """The ID of the current step."""
    frame_type: StackFrameType = StackFrameType.REGULAR
    """The type of the frame. Defaults to `StackFrameType.REGULAR`."""
    context: Optional[Dict[Text, Any]] = None
    """The context of the frame. Defaults to `None`."""

    @staticmethod
    def from_dict(data: Dict[Text, Any]) -> FlowStackFrame:
        """Creates a `FlowStackFrame` from a dictionary.

        Args:
            data: The dictionary to create the `FlowStackFrame` from.

        Returns:
            The created `FlowStackFrame`.
        """
        return FlowStackFrame(
            data["flow_id"],
            data["step_id"],
            StackFrameType.from_str(data.get("frame_type")),
            data["context"],
        )

    def as_dict(self) -> Dict[Text, Any]:
        """Returns the `FlowStackFrame` as a dictionary.

        Returns:
            The `FlowStackFrame` as a dictionary.
        """
        return {
            "flow_id": self.flow_id,
            "step_id": self.step_id,
            "frame_type": self.frame_type.value,
            "context": self.context,
        }

    def with_updated_id(self, step_id: Text) -> FlowStackFrame:
        """Creates a copy of the `FlowStackFrame` with the given step id.

        Args:
            step_id: The step id to use for the copy.

        Returns:
            The copy of the `FlowStackFrame` with the given step id.
        """
        return FlowStackFrame(self.flow_id, step_id, self.frame_type)

    def __repr__(self) -> Text:
        return (
            f"FlowState(flow_id: {self.flow_id}, "
            f"step_id: {self.step_id}, "
            f"frame_type: {self.frame_type.value}, "
            f"context: {self.context})"
        )
