"""Per-model native ownership and per-device bindings, not an algebra framework."""

from dataclasses import dataclass, field

import highspy

from open_energy_planner.optimization.domain import Interval


@dataclass
class DeviceBinding:
    """Own one device's native variables and expressions used by current component services."""

    device_id: str
    variables: dict[str, tuple] = field(default_factory=dict)
    ports: dict[str, tuple] = field(default_factory=dict)


class HighsModelContext:
    """Own all native variables and endpoint identities for one isolated model build."""

    def __init__(self, intervals: tuple[Interval, ...]):
        """Create a quiet native model and empty bindings for the supplied intervals."""
        self.intervals = intervals
        self.native = highspy.Highs()
        self.native.setOptionValue("output_flag", False)
        self.bindings: dict[str, DeviceBinding] = {}

    def require_binding(self, binding: DeviceBinding, device_id: str | None = None) -> None:
        """Reject bindings owned by another model or an unexpected device."""
        if self.bindings.get(binding.device_id) is not binding:
            raise ValueError("Binding belongs to a different model/scenario")
        if device_id is not None and binding.device_id != device_id:
            raise ValueError("Binding belongs to a different device")

    def register(self, device_id: str) -> DeviceBinding:
        """Allocate a fresh binding and reject empty or duplicate device IDs."""
        if not device_id or device_id in self.bindings:
            raise ValueError(f"Empty or duplicate device ID: {device_id}")
        binding = DeviceBinding(device_id)
        self.bindings[device_id] = binding
        return binding

    def binding(self, device_id: str) -> DeviceBinding:
        """Resolve a model-local device ID or raise ValueError for an unresolved reference."""
        try:
            return self.bindings[device_id]
        except KeyError as exc:
            raise ValueError(f"Unresolved device/connection: {device_id}") from exc

    def variables(
        self, binding: DeviceBinding, name: str, *, lb=0, ub=highspy.kHighsInf, binary=False
    ) -> tuple:
        """Allocate one bounded native variable per interval under a unique group name."""
        self.require_binding(binding)
        if name in binding.variables:
            raise ValueError(f"Duplicate variable group: {binding.device_id}.{name}")
        interval_count = len(self.intervals)
        variables = tuple(
            self.native.addVariables(
                interval_count,
                lb=lb,
                ub=1 if binary else ub,
                type=highspy.HighsVarType.kInteger if binary else highspy.HighsVarType.kContinuous,
                name=[
                    f"{binding.device_id}:{name}[{interval_index}]"
                    for interval_index in range(interval_count)
                ],
            )
        )
        binding.variables[name] = variables
        return variables
