"""JARVIS neural core: Synaptic Perceptron + brain-based 3D mesh + control layer."""
from .synaptic_perceptron import SynapticPerceptron, SynapticLayer, SynapseParams
from .brain_mesh import BrainMesh
from .control_layer import ControlLayer, Control, extract_features
from .scale import resolve_scale, ScaleConfig, NOMINAL_NEURONS

__all__ = ["SynapticPerceptron", "SynapticLayer", "SynapseParams", "BrainMesh",
           "ControlLayer", "Control", "extract_features", "resolve_scale",
           "ScaleConfig", "NOMINAL_NEURONS"]
