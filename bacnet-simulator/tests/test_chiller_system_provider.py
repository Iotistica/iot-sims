from src.simulation.models import ChillerModel, ChillerParameters
from src.simulation.providers.base import SimulationContext
from src.simulation.providers.system import PointBinding, SystemSimulationProvider


def make_provider() -> SystemSimulationProvider:
    model = ChillerModel(ChillerParameters(capacity_kw=500.0, nominal_cop=5.5))
    bindings = [
        PointBinding(101, "enable", "input"),
        PointBinding(102, "chw_return_temp_c", "input"),
        PointBinding(103, "chw_setpoint_c", "input"),
        PointBinding(104, "chw_flow_kg_s", "input"),
        PointBinding(105, "condenser_entering_temp_c", "input"),
        PointBinding(201, "run", "output"),
        PointBinding(202, "chw_leaving_temp_c", "output"),
        PointBinding(203, "cooling_kw", "output"),
        PointBinding(204, "power_kw", "output"),
        PointBinding(205, "cop", "output"),
        PointBinding(206, "plr", "output"),
    ]
    p = SystemSimulationProvider(model, bindings)
    p.initialize(SimulationContext(participant_device_ids=[1001], point_configs=[]))
    return p


def test_chiller_generates_correlated_outputs():
    p = make_provider()
    p.set_inputs({101: True, 102: 12.0, 103: 6.5, 104: 20.0, 105: 29.0})
    p.start()
    for _ in range(20):
        p.step(5.0)
    out = p.get_outputs()
    assert out[201] is True
    assert out[203] > 0
    assert out[204] > 0
    assert out[205] > 1
    assert 0 < out[206] <= 1
    assert 6.5 <= out[202] < 12.0


def test_hotter_condenser_water_uses_more_power_for_same_load():
    p = make_provider(); p.start()
    common = {101: True, 102: 12.0, 103: 6.5, 104: 20.0}
    p.set_inputs({**common, 105: 26.0}); p.step(5.0)
    cool = p.get_outputs()[204]
    p.reset(); p.start()
    p.set_inputs({**common, 105: 35.0}); p.step(5.0)
    hot = p.get_outputs()[204]
    assert hot > cool


def test_disabled_chiller_has_zero_cooling_and_power():
    p = make_provider()
    p.set_inputs({101: False, 102: 12.0, 103: 6.5, 104: 20.0, 105: 29.0})
    p.start(); p.step(5.0)
    out = p.get_outputs()
    assert out[201] is False
    assert out[203] == 0.0
    assert out[204] == 0.0
