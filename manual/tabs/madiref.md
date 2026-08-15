# MadiRef

Play a reference clip **in Blender's 3D viewport and in the app at the same
time**, frame-matched to the timeline — faster than Blender manages on the same
file.

---

## Why it is a separate tool

Blender is slow at video reference for four separate reasons, and MadiRef
addresses each one:

| Cause | Fix |
|---|---|
| Long-GOP H.264 — seeking frame *N* decodes from the previous keyframe | An **all-intra proxy**, where every frame is independent |
| Decoding on the main thread blocks the interface | Decoding happens **app-side, on a worker** |
| `Image.pixels` pushes float32 through the whole image system | Bypassed — drawn with the `gpu` module |
| The main thread is also evaluating your rig | It only does a texture upload |

**All video work happens outside Blender.** Blender uploads and draws pixels;
that is all.

---

## Placement

Three modes:

| Mode | What it is |
|---|---|
| **Viewport** | A screen-space overlay that follows the view |
| **Pinned** | A real quad left where you pinned it |
| **Camera** | A real quad riding the scene camera |

The two 3D modes get **depth for free** — they are real geometry, so Blender's
own depth test handles occlusion. Your character passes in front of the
reference exactly as you would expect.

There is a **lock** so a placed reference stays put while you work.

---

## Drawing notes

Park on a frame, press **Draw**, and mark it up — circle a hand, sketch an arc,
point at the thing you keep missing.

- Six pen colours and a **Width** slider.
- The button reads **Stop drawing** while the pen is up, so you always know
  which mode you are in.
- **Your markings show in Blender's viewport too**, on the reference itself, so
  they move, scale and rotate with it and go behind your character exactly as
  the picture does. **Show markings in Blender** turns that off for a clean
  reference while you animate; you can still see and edit them in the app.

!!! note "A drawing belongs to one frame"
    Draw on frame 120 and it is on 120. Draw on another frame and that is its
    own separate note.

**Finding them again**: every note puts a red tick under the scrubber, and
**Prev** / **Next** jump straight between them — which is how you land on a
single frame in a long clip.

They are **saved with the clip** and come back when you reopen it, even after
restarting the app. Your video files are never touched, and notes are kept apart
from the prepared clips, so clearing those never touches a drawing.

---

## The cache

Loading a clip prepares an all-intra proxy next to the app. That preparation is
what buys the speed.

- The disk the prepared clips use is shown under the button.
- **Close clip** frees every prepared clip in one go.
- The cache is budgeted in gigabytes, so it cannot grow without limit.
- Your original video files are never modified.

**Your clip is still there when you come back.** Close the app with a reference
loaded and it reopens with it — markings and all — with no waiting, because the
prepared copy is already on disk.

**Closing the app clears the reference from the viewport**, so Blender is never
left showing a picture nothing can move any more.

---

## Audio

Reference audio plays with the clip, and stays frame-matched to the timeline.
