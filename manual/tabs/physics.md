# Physics

## Bone Jiggle

Spring-driven motion on **bones** — hair, tails, ears, chains, cloth trim.

Each bone gets a simulated point pulled toward the pose the animation asks for,
and the bone is aimed at where the point ended up.

---

### Settings

**Per-end settings (tip and root) on their own sub-tabs**, so a chain can be
loose at the end and stiff at the base without a wall of controls.

**Dynamics** — stiffness, damping, mass, gravity, stretch.

**Collision**

- Analytic colliders: sphere, capsule, plane, box
- Real mesh colliders, or a whole collection of them
- Friction and bounce
- **Self collision** within a chain

**Wind** — plus Blender's force fields, all three types.

**Lateral links** between neighbouring chains, so a fringe moves as one.

---

### Baking

**Bake** to keyframes over a frame range with preroll.

A motion cache means scrubbing is not a re-simulation.

---

### Managing it

- Copy settings between bones.
- List and select what is already jiggling.

!!! warning "Do not run two jiggle systems on the same bone"
    If you also have a third-party jiggle add-on enabled, keep them off each
    other's bones. Both will write the same pose channels and the result is
    neither one.
