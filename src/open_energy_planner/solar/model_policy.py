"""Shared physical defaults without importing numerical or transport adapters.

DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE is a positive relative coefficient
per degree Celsius. Callers pass its negative as pvlib's gamma_pdc.
DEFAULT_GROUND_ALBEDO is the dimensionless ground-reflectance fraction.
"""

DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE = 0.004
DEFAULT_GROUND_ALBEDO = 0.2
