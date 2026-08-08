from src.energy.equipment.boiler import (
    BoilerEnergyConfig,
    BoilerEnergyModel,
    BoilerSnapshot,
)


config = BoilerEnergyConfig(
    rated_thermal_capacity_kw=2000.0,
    thermal_efficiency=0.92,
    rated_fuel_input_kw=2174.0,
)

model = BoilerEnergyModel(config)

snapshot = BoilerSnapshot(
    running=True,
    water_flow_liters_per_second=40.0,
    entering_water_temperature_c=60.0,
    leaving_water_temperature_c=80.0,
)

result = model.evaluate(
    snapshot,
    elapsed_seconds=5.0,
)

print(
    f"Heating output: "
    f"{result.thermal_output_kw:.2f} kW"
)

print(
    f"Fuel input: "
    f"{result.fuel_input_kw:.2f} kW"
)

print(
    f"Interval fuel energy: "
    f"{result.interval_fuel_energy_kwh:.4f} kWh"
)

print(
    f"Accumulated fuel energy: "
    f"{result.total_fuel_energy_kwh:.4f} kWh"
)

print(
    f"Auxiliary electric power: "
    f"{result.auxiliary_electric_power_kw:.2f} kW"
)

print(
    f"Interval electric energy: "
    f"{result.interval_electric_energy_kwh:.4f} kWh"
)

print(
    f"Accumulated electric energy: "
    f"{result.total_electric_energy_kwh:.4f} kWh"
)

print(
    f"Efficiency: "
    f"{result.effective_efficiency:.3f}"
)

print(f"Source: {result.source.value}")
print(f"Confidence: {result.confidence.value}")
print(f"Method: {result.method}")