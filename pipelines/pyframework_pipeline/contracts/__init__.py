"""Shared OOP contracts for the framework analysis pipeline."""

from .adapter import (
    DisassemblySpec,
    FrameworkAdapter,
    OperatorAnalysisAdapter,
    PerfAttachSpec,
    WorkloadHandle,
)
from .instruction import InstructionDataset, InstructionSample
from .records import PerfRecord, RawSample
from .step import RunContext, Step, StepError
from .tables import (
    AggregatedTables,
    CategoryRow,
    SharedObjectRow,
    SymbolRow,
)
from .timing import TimingDataset, TimingEntry

__all__ = [
    "AggregatedTables",
    "CategoryRow",
    "DisassemblySpec",
    "FrameworkAdapter",
    "OperatorAnalysisAdapter",
    "InstructionDataset",
    "InstructionSample",
    "PerfAttachSpec",
    "PerfRecord",
    "RawSample",
    "RunContext",
    "SharedObjectRow",
    "Step",
    "StepError",
    "SymbolRow",
    "TimingDataset",
    "TimingEntry",
    "WorkloadHandle",
]
