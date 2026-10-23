from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


from system_core import (BudgetComponent, Confidence,
                          RefusedTweakError, Risk,
                          TweakValidationError, Verdict)

MIN_MECHANISM_WORDS = 12

_VAGUE_MECHANISM = re.compile(
    r"\b(optimiz|boost|improv|enhanc|better|faster|smooth|maximiz|unlock|superior|"
    r"ultimate|turbo|snappy|responsiv)\w*\b",
    re.IGNORECASE,
)


class StepKind:
    REGISTRY_SET = "registry_set"
    REGISTRY_DELETE = "registry_delete"
    POWERSHELL = "powershell"
    NETSH = "netsh"
    POWERCFG = "powercfg"
    BCDEDIT = "bcdedit"
    SERVICE = "service"
    SCHEDULED_TASK = "scheduled_task"
    MANUAL = "manual"
    FILE_DELETE = "file_delete"
    WINGET_INSTALL = "winget_install"
    MEMORY_ACTION = "memory_action"


@dataclass(slots=True)
class Step:
    kind: str
    description: str
    hive: str | None = None
    path: str | None = None
    value_name: str | None = None
    value: Any = None
    value_type: str = "DWord"
    command: str | None = None
    requires_reboot: bool = False
    undo: tuple[Step, ...] = field(default_factory=tuple)
    text: str = ""
    notes: str = ""
    terminal: bool = False
    reversible: bool = True
    safety: str = ""
    restarts_service: str = ""

    def __post_init__(self) -> None:
        if self.kind == StepKind.MANUAL:
            if not self.text.strip():
                raise TweakValidationError(
                    f"MANUAL step {self.description!r} has no instructions"
                )
            return
        if self.kind in (StepKind.REGISTRY_SET, StepKind.REGISTRY_DELETE):
            if not self.hive or not self.path:
                raise TweakValidationError(
                    f"registry step {self.description!r} is missing hive/path"
                )
            if self.value_name is None:
                raise TweakValidationError(
                    f"{self.kind} step {self.description!r} is missing value_name"
                )
        elif self.kind in (
            StepKind.POWERSHELL,
            StepKind.NETSH,
            StepKind.POWERCFG,
            StepKind.BCDEDIT,
            StepKind.SERVICE,
            StepKind.SCHEDULED_TASK,
            StepKind.FILE_DELETE,
            StepKind.WINGET_INSTALL,
            StepKind.MEMORY_ACTION,
        ):
            if not self.command:
                raise TweakValidationError(
                    f"{self.kind} step {self.description!r} is missing command"
                )
        else:
            raise TweakValidationError(f"unknown step kind {self.kind!r}")
        if not self.reversible and not self.safety.strip():
            raise TweakValidationError(
                f"step {self.description!r} is irreversible and states no safety rationale"
            )
        if not self.undo and not self.terminal and self.reversible:
            raise TweakValidationError(
                f"step {self.description!r} has no undo; supply undo steps, mark "
                f"terminal=True, set reversible=False with safety, or use kind=MANUAL"
            )

    @property
    def is_manual(self) -> bool:
        return self.kind == StepKind.MANUAL

    @property
    def needs_reboot(self) -> bool:
        return self.requires_reboot or any(s.requires_reboot for s in self.undo)

    def undo_chain(self) -> list[Step]:
        out: list[Step] = []
        for u in self.undo:
            out.append(u)
            out.extend(u.undo_chain())
        return out


@dataclass(slots=True)
class Finding:
    verdict: Verdict
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.verdict.actionable


@dataclass
class Tweak:
    id: str
    title: str
    component: BudgetComponent
    mechanism: str
    confidence: Confidence
    risk: Risk
    summary: str
    probe: Callable[[Any], Finding]
    plan: Callable[[Any], list[Step]]
    expected_effect: str = ""
    evidence_urls: tuple[str, ...] = ()
    requires_reboot: bool = False
    experimental: bool = False
    anti_cheat_safe: bool = True
    reversible: bool = True
    tags: tuple[str, ...] = ()
    applies_to_os: tuple[str, ...] = ("windows",)
    conflicts_with: tuple[str, ...] = ()
    measure_metric: str = "p95"
    measure_min_effect_ms: float = 1.0
    measure_needs_load: bool = False
    measure_samples: int = 200
    caveats: str = ""
    refusal_reason: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.id or not re.fullmatch(r"[a-z0-9]+(?:[._][a-z0-9]+)*", self.id):
            raise TweakValidationError(f"tweak id {self.id!r} must be dotted lowercase")
        if not isinstance(self.component, BudgetComponent):
            raise TweakValidationError(f"{self.id}: component must be a BudgetComponent")
        m = (self.mechanism or "").strip()
        if len(m.split()) < MIN_MECHANISM_WORDS:
            raise TweakValidationError(
                f"{self.id}: mechanism must be at least {MIN_MECHANISM_WORDS} words, "
                f"got {len(m.split())}"
            )
        residue = _VAGUE_MECHANISM.sub(" ", m)
        if len(residue.split()) < max(4, len(m.split()) // 3):
            raise TweakValidationError(
                f"{self.id}: mechanism is marketing language, not a causal claim"
            )
        if not self.summary:
            raise TweakValidationError(f"{self.id}: summary is required")
        if not self.reversible and self.risk is not Risk.HIGH:
            raise TweakValidationError(
                f"{self.id}: irreversible tweaks must be rated Risk.HIGH"
            )
        if not self.anti_cheat_safe and self.risk in (Risk.NONE, Risk.LOW):
            raise TweakValidationError(
                f"{self.id}: not anti-cheat safe cannot be Risk.NONE/LOW"
            )
        if self.refusal_reason and self.risk is not Risk.HIGH:
            raise TweakValidationError(
                f"{self.id}: refused tweaks must be rated Risk.HIGH"
            )
        if self.experimental:
            if self.risk is not Risk.HIGH:
                raise TweakValidationError(
                    f"{self.id}: experimental tweaks must be rated Risk.HIGH"
                )
            if self.refusal_reason:
                raise TweakValidationError(
                    f"{self.id}: a tweak cannot be both experimental and refused"
                )
            if not self.caveats:
                raise TweakValidationError(
                    f"{self.id}: experimental tweaks must carry caveats"
                )
        if not re.fullmatch(r"p\d{1,3}", self.measure_metric) \
                and self.measure_metric not in ("mean", "max"):
            raise TweakValidationError(
                f"{self.id}: measure_metric must be a percentile (pNN), "
                f"'mean' or 'max'"
            )

    def evaluate(self, inventory: Any) -> Finding:
        try:
            finding = self.probe(inventory)
        except Exception as exc:
            return Finding(
                verdict=Verdict.UNKNOWN,
                summary=f"probe failed: {type(exc).__name__}: {exc}",
            )
        if not isinstance(finding, Finding):
            return Finding(
                verdict=Verdict.UNKNOWN,
                summary=f"probe for {self.id} returned {type(finding).__name__}",
            )
        return finding

    def build_plan(self, inventory: Any) -> list[Step]:
        try:
            steps = self.plan(inventory)
        except RefusedTweakError:
            raise
        except TweakValidationError:
            raise
        except Exception as exc:
            raise TweakValidationError(
                f"{self.id}: plan() raised {type(exc).__name__}: {exc}"
            ) from exc
        if not steps:
            raise TweakValidationError(f"{self.id}: plan() returned no steps")
        return list(steps)

    @property
    def is_refused(self) -> bool:
        return bool(self.refusal_reason)

    @property
    def needs_load_measurement(self) -> bool:
        return self.measure_needs_load or self.component is BudgetComponent.LOCAL_QUEUE


def validate_catalog(tweaks: Iterable[Tweak]) -> list[Tweak]:
    seen: dict[str, Tweak] = {}
    out: list[Tweak] = []
    for t in tweaks:
        if t.id in seen:
            raise TweakValidationError(f"duplicate tweak id {t.id!r}")
        seen[t.id] = t
        out.append(t)
    for t in out:
        for dep in t.conflicts_with:
            if dep == t.id:
                raise TweakValidationError(
                    f"{t.id} declares a conflict with itself")
            if dep not in seen:
                raise TweakValidationError(
                    f"{t.id} declares conflict with unknown tweak {dep!r}"
                )
    return out


@dataclass
class DebloatOp:
    id: str
    title: str
    summary: str
    reversible: bool = True
    safety: str = ""
    needs_elevation: bool = True
    requires_reboot: bool = False
    tags: tuple[str, ...] = ()
    not_a_latency_change: str = (
        "This is a removal or policy change, not a latency optimization. It frees resources "
        "or changes behaviour; it does not reduce ping or frametimes."
    )
    probe: Callable[..., Finding] | None = None
    plan: Callable[..., list] | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.id or not re.fullmatch(r"[a-z0-9]+(?:[._][a-z0-9]+)*", self.id):
            raise TweakValidationError(f"debloat id {self.id!r} must be dotted lowercase")
        if not self.title or not self.summary:
            raise TweakValidationError(f"{self.id}: title and summary are required")
        if not self.reversible and not self.safety.strip():
            raise TweakValidationError(
                f"{self.id}: irreversible operations must state a safety rationale"
            )
        if not self.not_a_latency_change.strip():
            raise TweakValidationError(
                f"{self.id}: must state that it is not a latency change"
            )


# ===== merged from common.py (Step factories) =====

def manual(description: str, text: str, notes: str = "") -> Step:
    return Step(
        kind=StepKind.MANUAL,
        description=description,
        text=text,
        notes=notes,
    )


def reg_set(
    hive: str, path: str, value_name: str, value: Any,
    value_type: str = "DWord", notes: str = "",
) -> Step:
    return Step(
        kind=StepKind.REGISTRY_SET,
        description=f"Set {value_name} in {hive}\\{path}",
        hive=hive,
        path=path,
        value_name=value_name,
        value=value,
        value_type=value_type,
        terminal=True,
        notes=notes or "Snapshot-restored on undo.",
    )


def reg_delete(hive: str, path: str, value_name: str, notes: str = "") -> Step:
    return Step(
        kind=StepKind.REGISTRY_DELETE,
        description=f"Delete {value_name} from {hive}\\{path}",
        hive=hive,
        path=path,
        value_name=value_name,
        terminal=True,
        notes=notes or "Snapshot-restored on undo.",
    )


def ps_step(
    description: str, command: str, undo_command: str = "",
    notes: str = "", requires_reboot: bool = False,
) -> Step:
    undo: tuple[Step, ...] = ()
    if undo_command:
        undo = (
            Step(
                kind=StepKind.POWERSHELL,
                description=f"Undo: {description}",
                command=undo_command,
                terminal=True,
            ),
        )
    return Step(
        kind=StepKind.POWERSHELL,
        description=description,
        command=command,
        notes=notes,
        requires_reboot=requires_reboot,
        undo=undo,
        terminal=not undo,
    )
