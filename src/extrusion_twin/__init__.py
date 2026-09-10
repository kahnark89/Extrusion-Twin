"""
extrusion_twin -- real-time melt-state digital twin for the 80" flexible-PVC sheet line.

    model       physics, parameters, state, the per-zone energy balances
    estimators  FixedGain (v1 corrector) and EnKF (ensemble Kalman filter)
    detectors   the fly circuit on the residuals: sign split, lag correlators, wide-field
                templates, single-zone detectors, cross-modal check, heater/contact checks
    cores       the same physics partitioned by anatomy, predictive-coding style
    io_bus      PXR-9 RS-485 bus, other Modbus devices, microcontroller analog line,
                PWA ops file, config loader, and the simulator
    corpus      alarm signatures -> CIAER+ labeled cases, with a nearest-case matcher
    run         the sweep loop (TwinRun), the batch form (run), and the command line
    api         the console's routes and run manager, with no transport attached to them
    server      HTTP in front of api, for the local console

Command line:  python -m extrusion_twin --help
"""
from .model import (ADAPTER, BARREL, CORES, DIE, N, SCREEN, ZONE_NAMES, Inputs, Plant, Recipe, State,
                    mv_expected, step)
from .estimators import EnKF, FixedGain, make_estimator
from .detectors import Bank, CrossModal, Detectors, EventEmitter, HeaterCheck, KappaAlarm
from .cores import Core, Council, CoreTwin
from .io_bus import EXAMPLE_CONFIG, LiveBus, PXRBus, SimPlant, build_plant_recipe, load_config
from .corpus import Corpus, Matcher, make_signature
from .api import StepRunManager, ThreadRunManager, TwinAPI, defaults
from .run import TwinRun, main, run

__version__ = "2.1.0"
__all__ = [
    "ADAPTER", "BARREL", "CORES", "DIE", "N", "SCREEN", "ZONE_NAMES",
    "Inputs", "Plant", "Recipe", "State", "step", "mv_expected",
    "FixedGain", "EnKF", "make_estimator",
    "Bank", "CrossModal", "Detectors", "EventEmitter", "HeaterCheck", "KappaAlarm",
    "Core", "Council", "CoreTwin",
    "PXRBus", "LiveBus", "SimPlant", "load_config", "build_plant_recipe", "EXAMPLE_CONFIG",
    "Corpus", "Matcher", "make_signature",
    "TwinRun", "run", "main",
    "TwinAPI", "ThreadRunManager", "StepRunManager", "defaults",
    "__version__",
]
