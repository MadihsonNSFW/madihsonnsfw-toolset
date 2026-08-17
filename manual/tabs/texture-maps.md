# Texture Maps

Turn one photo — or one texture already in your Blender scene — into a full set
of PBR maps: **Normal, Roughness, Ambient Occlusion, Height, Metallic, Bump**
and a **Seamless** tiling version.

Everything runs on your own graphics card. Nothing is uploaded anywhere, and
the tab works whether or not Blender is running.

## The short version

1. Drop an image on the tab, press **Browse…**, or press **Blender scene** and
   click one of your own textures.
2. Tick the maps you want. Four are ticked already.
3. Click a map's name to open its dials and adjust it while you watch.
4. **Export to folder…** or **Save ZIP…**

A `README.txt` goes out with the maps, explaining how to wire each one up.

## Using a texture from your Blender scene

Press **Blender scene** and the tab lists every image texture in the open
`.blend`, newest material first, with base colours at the front — those are
what you normally generate *from*. Click one and it becomes your source.

**Use active object** skips the list entirely: it takes the base colour of
whatever object you have selected in Blender.

A few things worth knowing:

- Images **packed into the .blend**, generated, or painted but not yet saved
  have no file to read. The Toolset asks Blender to write one out and uses
  that — you do not have to do anything.
- If you have shrunk a texture with the **Optimization** tab, the Toolset
  generates from **your original file**, not from the smaller stand-in, and
  says so under the file name. Maps made from a shrunken copy would look fine
  and carry a fraction of the detail.
- This part needs Blender add-on **0.50.0** or newer. On an older one the
  picker explains that and everything else keeps working — you can still open
  image files from disk.

## The maps

| Map | What it is for |
|---|---|
| **Normal** | Surface direction — the bumps and grooves, without extra geometry. Wire it through a Normal Map node. |
| **Roughness** | How polished the surface is. White is rough, black is a mirror. Not gloss — the opposite. |
| **Ambient Occlusion** | Where light gets trapped: mortar lines, fabric weave, dents. |
| **Height** | Real displacement. 16-bit by default. |
| **Metallic** | A mask: metal or not. Usually black for anything that is not actual metal. |
| **Bump** | Shading-only detail. A Normal map is better unless you specifically need a Bump node. |
| **Seamless** | Makes the *source* tile. Tick it and every other map is generated from the tiling version. |

Metallic and Bump start unticked on purpose. Most surfaces are not metal, and a
Normal map does Bump's job better.

## The preview

Four views, on the right of the chip row:

- **2D flat** — the map itself, tiled 1×, 2× or 3×.
- **Sphere** and **Cube** — your maps on a lit object. Drag to orbit.
- **All maps** — every map at once, so you can judge the set.

The **Preview material** dials — normal depth, AO intensity, displacement —
change *only what you are looking at*. They never touch the exported files, and
the group says so.

## Getting the maps into Blender

Every map except the source colour is **data, not a picture**. In Blender, set
each Image Texture node's colour space to **Non-Color**. The exported
`README.txt` repeats this, along with:

- **Roughness** and **Metallic** → the matching Principled BSDF inputs
- **Normal** → a Normal Map node → Principled **Normal**
- **Height** → a Displacement node → Material Output **Displacement**
- **Ambient Occlusion** → *not* a Principled input. Multiply it into your base
  colour with a Mix (Multiply) node. Never wire AO into a light.

Normal maps are **OpenGL** (+Y up), which is what Blender wants. Tick **Invert
Y (DirectX)** before exporting if you are heading for Unreal.

## Height: 16-bit or 8-bit

16-bit by default. An 8-bit height map has only 256 steps, and on a smooth
slope those steps are visible as banding — displacement is exactly where that
shows. The file is named `..._height16.png` so you can tell at a glance.

## If the tab says it needs OpenGL

It needs OpenGL 3.3, which effectively every graphics card made since 2010
supports. If your machine will not provide one — a remote desktop session is
the usual reason — this tab disables itself with a message and the rest of the
Toolset is unaffected.

## A note on what these maps are

They are **inferred from a photograph**, not measured from a real surface. They
are a fast, good-looking starting point. Check them against reference and
adjust rather than trusting them outright — especially Metallic, which is a
guess based on how colourless a pixel is.
