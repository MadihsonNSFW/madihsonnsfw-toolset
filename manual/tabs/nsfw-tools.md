# NSFW Tools

Ready-made MADI rigs, built into your scene in one click.

This tab is adult-oriented in intent; the rig itself is a general-purpose
geometry-node deformer. It sits **last in the sidebar**, below Physics, and
looks like every other entry — it used to be tinted pink, which made it the
one thing on screen that announced itself.

---

## Penetration Tech

A torus whose geometry-node rig **dents and bulges wherever a mesh in its
`Affectors` collection passes through it**.

It arrives as an ordinary, fully visible Geometry Nodes modifier:

1. Press the button — the rig is built into your scene.
2. Point its **Affectors** input at a collection of your own.
3. Tune it on the modifier like any other geometry node setup.
4. Bind it to your own mesh with a **Surface Deform** to drive that too.

!!! note "One button, no hidden state"
    There are no sliders in the app and nothing is hidden in your scene. The rig
    is a normal modifier from the moment it arrives, so everything you know about
    geometry nodes applies to it.

There is no simulation involved — the deformation is evaluated from the current
positions, so it scrubs cleanly in both directions.
