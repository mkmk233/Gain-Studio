from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot

from .cancellation import CancellationToken, ConversionCancelled


class FunctionWorker(QObject):
    finished = Signal(object)
    cancelled = Signal()
    failed = Signal(str)
    log = Signal(str)
    progress = Signal(int, str)

    def __init__(
        self,
        function: Callable[..., Any],
        *args: Any,
        cancel_token: CancellationToken | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.cancel_token = cancel_token

    @Slot()
    def run(self) -> None:
        try:
            signature = inspect.signature(self.function)
            parameters = signature.parameters
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            kwargs = dict(self.kwargs)
            if accepts_kwargs or "log" in parameters:
                kwargs["log"] = self.log.emit
            if accepts_kwargs or "progress" in parameters:
                kwargs["progress"] = self.progress.emit
            if self.cancel_token is not None and (accepts_kwargs or "cancel_token" in parameters):
                kwargs["cancel_token"] = self.cancel_token
            result = self.function(*self.args, **kwargs)
        except ConversionCancelled:
            self.cancelled.emit()
            return
        except Exception:
            self.failed.emit(traceback.format_exc())
            return
        self.finished.emit(result)
