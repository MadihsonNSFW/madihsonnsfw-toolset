"""Offline render queue engine for the MadihsonNSFW Toolset's Rendering tab.

Ported from the standalone Madi Offline Render Tool v1.7.0 (same author):
queue_tool.RenderQueueTool is the embeddable UI; render_controller drives
blender.exe headlessly via QProcess, so rendering never needs the bridged
Blender instance to be open.
"""
__version__ = "1.7.0"
