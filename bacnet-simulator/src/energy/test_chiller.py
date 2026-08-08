from .equipment.chiller import ChillerEnergyConfig, ChillerEnergyModel, ChillerSnapshot


def main() -> None:
    model = ChillerEnergyModel(ChillerEnergyConfig(rated_capacity_kw=3000.0, full_load_cop=5.8, iplv_cop=6.6, rated_electrical_power_kw=520.0))
    result = model.evaluate(ChillerSnapshot(running=True, load_percent=72.0, entering_water_temperature_c=12.0, leaving_water_temperature_c=6.5, water_flow_liters_per_second=95.0), elapsed_seconds=5.0)
    print(f"Cooling load: {result.cooling_load_kw:.2f} kW")
    print(f"Electrical power: {result.power_kw:.2f} kW")
    print(f"Interval energy: {result.interval_energy_kwh:.4f} kWh")
    print(f"Accumulated energy: {model.total_energy_kwh:.4f} kWh")
    print(f"Source: {result.source.value}")
    print(f"Confidence: {result.confidence.value}")
    print(f"Method: {result.method}")


if __name__ == "__main__":
    main()
