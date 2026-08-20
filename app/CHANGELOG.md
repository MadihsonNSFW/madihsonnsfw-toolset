# MadihsonNSFW Toolset — everything it does

A companion app for Blender. It runs in its own window next to Blender and
drives it over a small local bridge, so your tools stop competing with the
viewport for screen space. A few parts (the render queue, the library browser,
importing) work with Blender closed entirely.

**Thirteen tabs, and every one of them is free.** Nothing in the Toolset needs a
licence, an account, or an internet connection. It is open source, and the
Toolset makes **no network connections at all**.

---

## Update notes

### 1.24.0 - Rig properties: drive and key a rig's morphs from the app

The Organize tab is now two pages: **Isolate** (everything it did before) and
a new **Rig properties**. Click a rig and every custom property it carries -
Daz morphs, controllers, switches - is listed as a channel: its value, and
where its keyframes sit on the timeline.

- **Filter and sort hundreds of properties.** Type to search, or narrow to
  **Animated**, **Keyed here** or **Non-zero**. A Daz character carries around
  775 of these; finding the four you want was the whole problem.
- **Drag any value** and the rig moves in Blender as you drag - or
  **double-click it and type an exact number**. Typing can go outside the
  slider's range, the way Blender's own fields do.
- **Keyframe from the app.** The diamond at the end of a row inserts a key on
  the current frame, or deletes the one already there. Select several rows and
  **Key**, **Delete key**, **Delete all keys** or **Reset** them together.
- **See where the keys are.** Every row draws its keyframes on the scene's
  frame range, with the playhead marked. Click a diamond in a strip to jump
  there, or use the arrows either side of the frame number to step from key to
  key.
- **It follows the rig you click** in Blender, or pin one with the picker.
- Colours match Blender's own: green means the property is animated, yellow
  means it is keyed on the frame you are on.

⚠ **Update the Blender add-on for this one** (⚙ Settings ▸ **Update add-on**).
The tab needs add-on **0.52.1**; on an older one it greys itself and says so,
and nothing else is affected.

**Also in this release:**

- **More tools grey out while Blender is rendering.** The tools on the Anim
  Layers, Rendering and Node Setup rails stayed fully clickable during a
  render and quietly did nothing. They disable properly now. (The Physics tab
  already behaved.)
- **Drawn icons follow a theme change.** Switching theme left every drawn
  button glyph in the old palette until the tab was rebuilt.

### 1.23.0 - Organize: sets of objects, and one key to see just them

A new tab under Scene. Pick some objects in Blender, press **New set from
selection**, and you have a named set - a rig with its meshes, the lights for
a shot, the props on a table. Press **Isolate** and the viewport shows only
that set, exactly as if you had selected it and pressed **/**. Press it again
to come back out.

- **The sets live inside your .blend.** Rename the file, rename the objects,
  send it to someone else - the sets travel with it. Nothing is stored in the
  app.
- **It is in Blender too.** The same list, with the same isolate star on each
  row, is in the sidebar (N key) under **MadihsonNSFW ▸ Organize**. Change it
  in either place and the other follows.
- **Isolate is Blender's Local View** - the same thing as selecting the set
  and pressing **/**. Nothing in your scene is hidden or changed, and your
  renders are untouched; it is only what the viewport is showing. Press it
  again to come back out.
- **Nothing is lost when an object goes.** Delete an object that is in a set
  and the set says so, with a Clean button, rather than quietly forgetting it.
- **Select in Blender** selects the whole set and makes its rig active.

**Also in this release:**

- **NSFW Tools is no longer pink**, and it has moved to the bottom of the
  list. It looks like every other tool now.
- **The Texture Maps chips were redrawn.** Each tick sits inside its own chip,
  the chip you are looking at is outlined rather than filled, and the row
  wraps onto a second line instead of squashing the view buttons when the
  window is narrow.
- **Greyed-out blue buttons now look greyed out.** A disabled accent button -
  Apply in the library, Start in the render queue, Build in Node Setup - kept
  its full colour and looked pressable.

### (folded in) 1.22.1 - Texture Maps: the map chips look like what they are

The row of map chips above the preview was drawn wrong: each tick appeared
to float outside its own chip, the selected chip turned into a solid blue
block that swallowed its tick, and on a narrower window the view buttons were
crushed into "Sp…re" and "Al…p".

- **One box per chip, tick inside it.** The chip you are looking at is now a
  tinted chip with a blue outline, so the tick stays readable on it, and the
  view buttons on the right keep their full names at every window size.
- **The row wraps instead of shrinking.** Make the window narrow and the chips
  move to a second line; nothing is squeezed, cut off, or scrolled.

### 1.22.0 - Texture Maps: a photo becomes a material

A new tab. Drop in a photo, or take a texture straight out of your Blender
scene, and get a full PBR set out of it - **Normal, Roughness, Ambient
Occlusion, Height, Metallic, Bump** and a **Seamless** tiling version.

- **It runs on your graphics card.** A map re-renders in about six
  milliseconds, so the preview keeps up with the slider rather than catching
  up after it.
- **Use the textures you already have.** Press **Blender scene** and every
  image texture in the open .blend is listed - base colours first, because
  that is what you generate from. **Use active object** skips the list
  entirely. Images packed into the .blend or painted and unsaved are handled
  for you.
- ⚠ **If you have shrunk a texture with the Optimization tab, the maps are
  made from your ORIGINAL file**, not from the smaller stand-in, and the tab
  says so. Maps built from a shrunken copy look perfectly fine and carry a
  fraction of the detail.
- **Tick what you want, look at what you like.** A map's tickbox decides what
  gets exported; clicking its name opens its dials. They are separate on
  purpose.
- **Four views**: the flat map (tiled 1x/2x/3x), a lit sphere and cube you can
  orbit, and every map at once.
- **16-bit height maps** by default, named `..._height16.png`, because an
  8-bit one bands visibly on smooth slopes.
- **Export to a folder or a ZIP**, with a README explaining how each map wires
  up in Blender, Unreal and Unity - including the one everybody gets wrong
  (every map except the base colour is **Non-Color**).
- Normal maps are OpenGL (+Y), which is what Blender wants. There is an
  **Invert Y (DirectX)** tick for Unreal.

The tab needs OpenGL 3.3. If your machine will not provide one, the tab says
so and nothing else is affected.

Taking textures out of Blender needs add-on **0.50.0**. On an older add-on the
scene picker explains that and everything else keeps working - the tab does
its whole job from a file on disk with Blender closed.

### 1.21.1 — the "Update add-on" button actually updates the add-on

Two bugs in the same corner, both about the Blender add-on install. The
installing itself was never broken — only what the app told you about it.

- **"Update add-on" now installs the add-on.** The button beside the version
  warning used to open the Settings window instead, leaving you to find
  **Install in Blender** inside it. It goes straight to the confirmation now.
- **"Installing Blender add-on…" now stops when it finishes.** The progress
  strip never came down, so a completely successful install looked like it had
  hung. If you saw that: the add-on almost certainly installed fine.
- **A failed install now says so.** The warning that reports a failure was
  unreachable, which meant success and failure looked exactly alike.

### 1.21.0 — Quadify is back

**Quad remeshing returned to the Optimization tab**, exactly as it worked
before: density, sharp-edge detection, symmetry, your original kept and hidden,
Blender usable while it runs, and cancel. It was removed in 1.20.0 and restored
the same day.

⚠ **Update the Blender add-on for this one** (⚙ Settings ▸ **Update add-on**).
Quadify's Blender half — the remeshing engine and its commands — lives in the
extension, so the tool cannot run against an older one. The app expects add-on
**0.49.0**.

### 1.20.0 — Windows only

Housekeeping, and one thing worth saying plainly: **the Toolset is a Windows
application.** A Linux and macOS port was explored and cancelled, so there is
no ambiguity about what is supported.

### 1.19.0 — open source, and the app stops phoning home

**The Toolset is now on GitHub, and it no longer updates itself.**

- **Nothing is sent anywhere, ever.** The licence check, the sign-in, the seat
  system and the automatic update check are all gone — not disabled, removed.
  The app talks to Blender on a port on your own machine and to nothing else.
- **New versions come from GitHub.** Download a release, close the app, unzip it
  over your folder. Your library, render queue, presets and baked maps live
  outside the program folder, so an update never touches them.
- **No more licence chip, Updates button, or "Check for updates" setting.**
  There is nothing left for them to report.
- **⚙ Settings ▸ Update add-on still works exactly as before.** That was never
  a server feature — the app carries the Blender extension inside itself and
  hands it straight over. It is the one thing that survived the clear-out.
- **The Blender add-on drops its licence gate too** (0.47.0). No panel can say
  "locked" any more, because there is nothing to lock with.

⚠ **Update the Blender add-on for this one** (⚙ Settings ▸ **Update add-on**).
The app and the extension are a matched pair, and 0.47.0 is where the gate is
removed on Blender's side.

**Where to get it:** <https://github.com/MadihsonNSFW/madihsonnsfw-toolset>

### 1.18.1 — the window has its own title bar, and the app starts faster

- **The app opens noticeably quicker.** Three internal checks were being run
  against everything that happened anywhere in the app; they now run only where
  they are needed. Building the main window got about **three times faster**.
- **Big libraries open far faster.** Opening or refreshing a library used to
  read every single item's data file to fill the "Any author" dropdown. It now
  fills that list when you open it, so a 200-item library opens in a quarter of
  the time — and the bigger the library, the bigger the difference.
- Less memory at rest: the GPU monitoring library is no longer loaded unless
  the Render Queue's stat cards actually need it.
- **The app looks like an app now, not a stock Windows window.** The sidebar
  runs all the way to the top and carries the MADI mark; beside it the bar
  tells you which section you are in, with the minimise, maximise and close
  buttons in the corner you expect them.
- **Everything Windows does still works.** Drag the bar to move the window,
  drag it to the top edge to maximise, double-click it to do the same, snap it
  to either side, and resize from any edge or corner.
- **Typing in the library search is instant now, even in huge libraries.**
  Filtering shows and hides tiles instead of rebuilding the whole grid — at
  800 items a single keystroke used to freeze the app for almost half a
  second; now you cannot feel it.
- **Thumbnails load as they come into view.** A big library opens right away
  and fills in around you; zooming the tiles no longer re-reads every
  thumbnail from disk, and the preview memory stays inside a fixed budget
  however large the library grows.
- **The Optimization tab no longer talks to Blender in the background.** Its
  status check runs only while the tab is on screen — one less thing waking
  your machine every few seconds while you animate.
- **Tabs are built the moment you first open them**, starting with Anim
  Layers. The window appears sooner, and a session that never opens a tab
  never pays for it.
- **Updating the Blender add-on tells you the truth when you have two Blenders
  open.** The add-on goes into whichever Blender is running the bridge — and
  because installing makes that Blender reload, the other one can pick the
  bridge up mid-update. That used to end in "Blender stopped answering",
  which was never true and pointed at the wrong fix. Now it says which Blender
  actually got the add-on and tells you to start the bridge in the one you
  wanted, then press Update again.

### 1.17.3 — clearer group headings

- **ANIMATION, NODES and SCENE now sit on their own shaded bar**, so a heading
  and a tool can never be mistaken for each other at a glance.

### 1.17.2 — the sidebar groups open properly

- **The group headings look like headings you can open.** ANIMATION, NODES and
  SCENE now show an arrow: pointing down when open, sideways when closed.
- **One click opens them, not two.** They could always be collapsed, but it
  took a double-click and there was nothing on screen to suggest it.

### 1.17.1 — the window gets small

- **You can finally shrink the window.** It used to refuse to go below roughly
  980 x 900, which took up almost a whole 1080p screen; it now goes down to
  about 550 x 590. Panels that used to force the window wide — MadiRef's
  controls, the library sidebar, every tool's settings pane — scroll instead.
- **The sidebar shrinks with it.** Drag the window narrow and the section list
  slims down to just its icons rather than clipping the names in half. Hover
  any of them for the name.
- **The render queue always has a job selected.** If nothing is selected it
  falls back to the first one, so Start, the frame range and Remove are never
  quietly pointing at nothing.

### 1.17.0 — a new look, and updates that actually install

- **Updating works again.** Installing an update could fail part way through
  and quietly undo itself, then try again forever — some people had never had
  a single update land. Fixed, and this build is the one that gets you out of
  it.
- **The tabs moved to a sidebar.** All eleven sections now live down the left
  in one grouped list instead of a cramped row across the top: the library and
  rendering at the top, then Animation, Nodes and Scene. Everything is in the
  same order it was, and the page you are on gets more room.
- **Real icons everywhere.** Every button that used to be a little symbol —
  rescan, settings, playblast, import, watch — is now a proper drawn icon that
  matches whichever theme you are using, instead of borrowing whatever your
  system had lying around.
- **The window narrows further than before.** A single long heading had been
  holding it open; it now fits comfortably on a smaller screen even with the
  new sidebar.

### 1.16.0 — every tab is free

- **All four paid tabs are now free for everyone.** MadiRef, Optimization,
  NSFW Tools and Physics no longer need a licence — no gold stars, no lock
  screens, no signing in. Everything the Toolset does is open.
- **Inside Blender too.** The bridge no longer refuses MadiRef or Scene
  Optimizer commands without a licence, and the Optimization
  viewport preview works for everyone. Update the add-on when the app offers
  it, or the old locks stay behind in Blender.
- **Your key still matters.** Premium pose and animation packs are coming in
  a new MADI Packs tab — one key will unlock every pack.

### 1.15.1 — the Blender add-on installs again

- **Fixed: "Install in Blender" did nothing at all.** The add-on package in
  1.15.0 was one Blender refuses to read, so pressing Install quietly changed
  nothing — no error, no message, and the app sat on "Updating…" until it gave
  up and blamed Blender. If you are on 1.15.0, this is the update that fixes
  it. **Update the app first, then install the add-on.**
- **It can no longer fail silently.** The app now checks the package before
  handing it to Blender, Blender's own files are read back afterwards to
  confirm what really got installed, and if anything goes wrong you are told
  what happened instead of watching a spinner.
- **Better messages when an install does not finish.** If the add-on installs
  but Blender's bridge does not reconnect on its own, the app says so and
  tells you to press Start in Blender's sidebar — rather than suggesting the
  update failed, which it had not.

### Render Queue — renders now always get their file extension

- **Fixed: frames could be written with no extension at all** — `0298` instead
  of `0298.png`. It happened when the .blend had **File Extensions** unticked
  in Output properties, which Blender obeys unless it is told otherwise. The
  queue now always tells it otherwise.
- **This also made finished renders look Failed.** Preview Video, View Anim.
  and the output check all find files by extension, so a sequence that
  rendered perfectly was invisible to the tool that made it.
- **If you already have a folder of extensionless frames, they are fine** —
  they are valid images. Rename `0298` to `0298.png` and it opens.
- **Every output format is covered now**, not just the common ones — Cineon,
  JPEG-2000, IRIS, SGI and Radiance images, and MPEG, FLV, DV and OGG video.

⚠ **Needs the Blender add-on updated to 0.45.0.**

### 1.14.0 — Layers that really hide, and saved marker sets

- **Choosing a layer now hides the other markers from Blender's timeline
  itself**, not just from the lists. Pick a layer and the strip clears down to
  that layer's markers; choose "All layers" and everything comes back exactly
  as it was, notes, tags and camera bindings included. Both windows tell you
  how many are hidden while it is happening.
- **Save marker sets into your .blend.** Name the current markers — "Shot
  breakdown", "Audio beats" — keep as many as you like, and swap between them.
  They live in the file, so they travel with the project and the app simply
  lists whatever the open file holds.
- **The marker list reads better.** Each row is two lines now: the name, layer
  and frame on top, the note underneath — so you can read every note at a
  glance instead of clicking each marker in turn.
- **The Blender panel is tidier.** The list is the panel; everything about the
  selected marker moved into a *Marker details* section you can collapse.

⚠ **Needs the Blender add-on updated to 0.43.1.**

Worth knowing: hiding is a view, not a saved state — opening a .blend always
shows every marker, so a file opened without the add-on can never look like it
lost them. Loading a saved set replaces every marker in the scene, and asks
first.

### 1.13.1 — Markers where you actually work

- **The Timeline Markers panel is now in the 3D viewport sidebar too**, in the
  MadihsonNSFW tab alongside Studio Library — press N in the viewport. It is
  still in the Timeline and Dope Sheet sidebars as well, so use whichever is
  in front of you. It is the same panel and the same markers either way.

⚠ **Needs the Blender add-on updated to 0.42.0.**

### 1.13.0 — Marker layers

- **Every marker can now belong to a layer** — Blocking, Polish, Sound,
  whatever you name them. Pick a layer and the list shows only that layer's
  markers; pick none and they all show, including any that are not on a layer.
- **Creating a layer is just typing its name** on a marker. There is no list to
  set up first, and a layer disappears when nothing uses it any more.
- **A marker you add while a layer is selected joins that layer**, so it does
  not vanish the moment you create it.
- Works in both windows — the app's Markers tool has a Layer column and a Layer
  box, and the Blender panel has a layer menu at the top.
- Note: this filters the **lists**. Blender always draws every marker on the
  timeline ruler itself, so the pins there do not change.

⚠ **Needs the Blender add-on updated to 0.41.0.** The layer controls grey
themselves out until you do, rather than looking like they work.

### 1.12.0 — Timeline markers that can hold a note

- **A new Markers tool** in the Anim Layers tab, and a **Timeline Markers
  panel** inside Blender (Timeline or Dope Sheet, press N, MadihsonNSFW tab).
  Both are free, and both edit the same markers — change something in one and
  it shows up in the other.
- **Every marker can carry a note and tags.** Write what you want to remember
  about a frame — *"hips lead the step"* — and it stays on the marker. Tag
  them `hero`, `wip`, `fx`, then filter the list down to just those.
- **Search reaches the note and the tags**, not only the name, so you can find
  a frame by what you said about it.
- **The notes travel with the file.** They are stored on the marker inside the
  .blend, so they survive saving, reopening, and being opened on a machine
  that does not have the add-on at all.
- **Jump to any marker** from the list, or double-click it.
- **Render at marker** saves your file and adds that single frame to the
  Render Queue. It does not start rendering — the queue's own buttons do that,
  and it renders in the background so Blender stays free.
- **Bind cameras by name** in one click: every marker named after a camera gets
  bound to it.
- **Batch rename** with a prefix, a suffix, or find-and-replace.
- **Import and export** your markers as a file, to move them between .blend
  files or into other software. It is plain JSON, and it will read marker files
  exported from elsewhere too.
- Editing in both windows at once is safe: a note you are halfway through
  typing is never overwritten by a refresh.

⚠ **This one needs the Blender add-on updated to 0.40.0** — the tool tells you
so and greys itself out until you do. Update it from ⚙ Library Settings.

### 1.11.0 — See what is making your .blend so big

- **A new File size tool** in Optimization ▸ Maintenance. It opens your .blend
  and adds up what is really inside it, biggest first: every mesh, image,
  shape key and object **by name**, and — open one up — what that datablock is
  actually made of. Shape keys, vertex groups, Surface Deform bind data,
  packed images, animation curves.
- **These are exact figures, not estimates.** They are read from the file
  itself rather than guessed at from the scene, so an uncompressed .blend adds
  up to precisely its size on disk.
- **It sees things the Memory report cannot.** That one estimates what the
  render needs in RAM and walks meshes and textures only. This one sees
  everything the file holds, including datablocks nothing in your scene is
  using any more.
- **Blender does not need to be running.** The tool reads the file off disk, so
  it works with Blender shut, and **Choose a file…** will measure any .blend on
  your drive, not just the one you have open.
- Compressed .blend files are read directly — no unpacking, no temporary copy.
  A 650 MB file takes a few seconds, with a progress bar, and the rest of the
  app stays usable while it reads.
- Because Blender compresses .blend files by default, the sizes inside add up
  to more than the file in Explorer. Both totals are shown side by side.

**Two fixes worth reading.**

- **Paid Blender features could be locked for no reason.** If your Blender
  add-on was not the exact version this app expects — which happens any time
  you update one and not the other — everything paid that runs *inside*
  Blender refused to work: the Optimization tools, MadiRef's viewport
  reference. You would see *"The Scene Optimizer is locked — open the
  MadihsonNSFW Toolset app and sign in"* with the app open and connected right
  in front of you. The app was never sending your licence through. It does
  now, whatever version your add-on is.
- **A crash when playing a reference clip with drawings on it.** Any frame
  carrying a marker note could take the whole app down. Fixed. If you have
  1.10.0, this is the reason to update.

⚠ **Update the Blender add-on for this one too** (⚙ Settings ▸ **Update
add-on**). It goes to 0.39.0, where **the bridge no longer starts itself** —
you start it per Blender from the N-panel, and a second Blender cannot take it
from underneath the one you are working in.

### 1.10.0 — Draw on your reference, and a bridge you start yourself

**Update the Blender add-on for this one** (⚙ Settings ▸ **Update add-on**) —
the bridge change below lives in the extension.

- **Draw notes straight onto a reference clip.** Park on a frame, press
  **Draw**, and mark it up — circle a hand, sketch an arc, point at the thing
  you keep missing. Six pen colours and a **Width** slider. The button reads
  **Stop drawing** while the pen is up, so you always know which mode you are
  in.
- **Your markings show in Blender's viewport too**, on the reference itself, so
  they move, scale and rotate with it and go behind your character exactly as
  the picture does. **Show markings in Blender** turns that off for a clean
  reference while you animate; you can still see and edit them in the app.
- **A drawing belongs to one frame and shows only there.** Draw on frame 120
  and it is on 120 — draw on another frame and that is its own separate note.
- **Find them again.** Every note puts a red tick under the scrubber, and
  **Prev** / **Next** jump straight between them, which is how you land on a
  single frame in a long clip.
- **They are saved with the clip** and come back when you reopen it, even after
  restarting the app. Your video files are never touched, and notes are kept
  apart from the prepared clips — clearing those never touches a drawing.
- **Closing the app clears the reference from the viewport**, so Blender is
  never left showing a picture nothing can move any more.
- **Your clip is still there when you come back.** Close the app with a
  reference loaded and it reopens with it — markings and all — with no waiting,
  because the prepared copy is already on disk.
- **One button opens and closes.** It reads **Close clip** while one is loaded;
  closing frees every prepared clip from disk in one go, and the disk they use
  is shown right under it. Your video files are never touched.
- **Menus highlight under the cursor again.** Right-click menus and the node
  editor's Shift+A list had no hover highlight at all.
- **The Blender bridge no longer starts itself.** Start it from the sidebar,
  once per Blender. If another Blender already has it, the second one now says
  so instead of quietly taking it over the moment the first one stops — so the
  app can no longer end up driving a different file than the one you are
  looking at.

### 1.9.0 — MadiRef is now members-only

MadiRef has moved behind the licence, alongside Optimization, NSFW Tools and
Physics. It stays where it always was in the tab strip, now with the gold star.
Everything it does is unchanged — the notes below still describe it in full.

### 1.8.0 — MadiRef: video reference, in Blender and here at once

**Update the Blender add-on for this one.** ⚙ Settings ▸ **Update add-on**
installs the version the viewport half needs.

- **Open a reference clip and it plays in Blender's 3D viewport**, over your
  scene, while you animate. The first time you open a clip it is prepared once
  — after that, scrubbing lands on any frame instantly, which is the part
  Blender itself is slow at.
- **It follows your timeline by TIME, not by frame number.** A 60 fps clip on a
  24 fps scene shows the right moment instead of playing in slow motion. Set an
  **Offset** to line the reference up with your shot, and a **Speed** to retime
  it.
- **Hold real time.** With this on, a heavy scene drops frames instead of
  playing everything slowly, so the reference runs at true speed and still
  matches the frame you are on. Your own sync setting is put back when you
  close the reference.
- **It adds nothing to your scene.** There is no object to delete, save or
  accidentally render — the reference is drawn straight into the viewport and
  it is gone the moment you close it.
- **Place it with the mouse, in the viewport.** Drag the picture to move it,
  drag a corner to scale it, and drag the knob above it to rotate. The wheel
  over it scales too. Hold **shift** for fine control, **ctrl** while rotating
  to snap to 15°, and right-click mid-drag to cancel. The sliders on the tab
  follow along, and **Reset placement** puts it back if it ever gets lost.
- **The same frame shows here in the app**, at the same moment.
- **Put your scene in front of it.** The **Depth** slider under *Keep in front*
  sets how far away the reference sits, in metres. Anything nearer than that
  covers it, anything further is behind it — fully shaded, per-pixel exact,
  using Blender's own depth. Slide it through your character and the reference
  passes behind them. At 0 it is off and the reference stays on top.
- **Three ways to place it.** *Follows the viewport* keeps it on screen as you
  navigate. *Pinned where you put it* drops it into the scene at its current
  spot, so you can orbit around it. *Pinned to the camera* rides the camera —
  only the camera moves it, and Depth sets how far in front it sits.
- **Lock it** when you are happy, and it stops reacting to the mouse entirely.
- **Prepared clips are kept** so reopening one is instant. The panel shows how
  much disk they use, and they are trimmed automatically to stay under the
  budget in ⚙ Library Settings. Your original video files are never touched.
- **Audio, if you want it.** Off by default. It plays while the timeline runs
  and stays quiet while you scrub.

### 1.7.0 — Bone picker and the Render Queue

**Update the Blender add-on with this one.** ⚙ Settings ▸ **Update add-on**
installs 0.34.0, which three of these five need. The app will tell you if you
try one of them without it.

**Bone picker**

- **Buttons no longer collide.** Moving or resizing one over another used to be
  blocked; now nothing stands in the way. Aligning still spreads a row apart,
  because that is what you asked it to do.
- **Scale one button on its own.** Pick its row in the button list and use the
  new **Scale** slider underneath. The Button Scale brush above still works on
  the whole selection — which is what you want for many buttons at once, and
  not what you want for a single round group handle.
- **Bones & Extras.** One button hides bones, empties and cameras in every 3D
  viewport, so you can pick from the picture with the character clear of
  controls. Press it again to bring them back; it shows which way it is set.

**Render Queue**

- **Save & Queue.** Takes the .blend you have open in Blender, saves it, and
  adds it to the queue. The queue renders files on disk, so this is what stops
  it rendering the version you saved an hour ago. Press it again after more
  work and it re-uses the same row instead of stacking duplicates. A file that
  has never been saved is refused rather than written somewhere arbitrary.

**Studio Library**

- **Save Picker Tab** joined the other save buttons, so a picker layout can be
  filed without leaving the library. The preview is the tab's reference picture
  with the buttons drawn on, as it is everywhere else.

---

## Getting set up

1. **Run the app.** No installer — unzip it anywhere and run
   `MadihsonNSFW Toolset.exe`.
2. **Install the Blender add-on.** ⚙ Settings ▸ **Update add-on** installs it
   straight into Blender for you; the app carries the matching version inside
   itself, so the two can never be out of step. You can also install the
   `madi_anim_library-*.zip` by hand through Blender's Preferences ▸ Add-ons.
3. **Start the bridge.** In Blender's 3D viewport, press **N** and open the
   **MADI** panel. The bridge listens on port 9877 on your own machine and
   nowhere else. There is an **Open Toolset App** button there too — the first
   time you press it, Blender asks you to point at the exe, and remembers
   where it is. Nothing about where you keep the app is assumed.

   That panel is the *connection*, not a second copy of the app: start and
   stop the bridge, open the Toolset, watch your last render. Saving and
   applying happen in the app, where the thumbnails and folders are.
4. The status bar at the bottom of the app tells you what it is connected to,
   including **which .blend** — useful when two Blenders are open, since only
   one can hold the bridge at a time.

Where your library lives is up to you: set it in the app's ⚙ Settings, and in
the add-on preferences if you also use the panel inside Blender. Left alone,
both use a `library` folder next to the app.

Blender 5.x. Windows.

---

## Studio Library

A shared library for anything you reuse, browsable as a grid of thumbnails.

**Ten kinds of item, all saved and applied the same way:**

- **Poses** — a pose on the selected bones, or the whole rig.
- **Animations** — with an options dialog: the frame range (pre-filled from
  your scene), **bake every frame**, **keep F-curve modifiers** so Noise and
  Cycles survive the round trip, and **inherit every bone property** so IK/FK
  and space switches come back set the way they were.
- **Selection sets** — a named set of bones, re-selected in one click.
- **Mirror tables** and **bone remaps** — for moving animation between rigs.
- **Shape keys** — pick exactly which keys to save, with filters (driven keys
  are excluded by default, which matters on a DAZ figure where nearly every
  key is driven).
- **Alembic caches** — exported through a dialog carrying Blender's full
  Alembic option set: frame range, selected-only, flatten hierarchy,
  instancing, UVs / normals / colour attributes / generated coordinates /
  face sets / custom properties, curves as mesh, subdivision, scale,
  triangulation, hair and particles, render-vs-viewport evaluation, and
  sub-frame sampling. What was actually used is written into the item.
- **Vertex groups** — choose which groups to save from a searchable checklist,
  or write one item per group in a single pass.
- **Bone picker layouts** — see the Bone picker tab.
- **Render presets** — see Rendering.

**Browsing and organising**

- Thumbnails: a viewport capture, a playblast, or an automatic **weight-paint
  render** for vertex groups. Bone picker layouts draw their actual buttons
  onto the reference picture.
- Hover a multi-item tile and it plays through what it holds. Multi-item items
  carry a stack badge and a count; animation tiles carry small marks for baked
  / kept F-modifiers / stored bone properties.
- Folders nest as deep as you like. Tags, colour labels, a search box, and a
  type filter down the left side — **press and drag down the filter list** to
  tick or untick a run of them in one gesture.
- With nothing selected, the details panel gets out of the way instead of
  showing you a column of dashes.
- **Save Picker Tab** sits with the other save buttons, so a picker layout can
  be filed without leaving the library.
- **Versions** — overwriting an item keeps the old one. Nothing is lost.
- **Zip for sharing** — select any number of items, right-click, and get an
  archive that unzips straight back into someone else's library.
- **Import** — hand it a zip, a folder, loose items, or a pile of `.abc` /
  `.mp4` files. It shows you what it found before copying anything, recreates
  folder structure, brings thumbnails / previews / tags / colours / versions
  with it, and **never overwrites**: a name already taken gets a number.
  Works with Blender closed.
- **Previews are captured with overlays off** — no bones, wires, gizmos or
  grid floor in your thumbnails — and your viewport is put back exactly as it
  was.
- **Playblasts** — record one from the tab, in the background if you like,
  defaulting to your scene's own output folder and the active camera. **▶**
  plays the newest one; there is a matching **Watch last render** button in
  Blender's own panel.

---

## Rendering

Three tools.

**Render Queue** — renders with Blender closed.
Queue stills, animations and playblasts across scenes and .blend files, watch
progress frame by frame, pause and resume, and disable whole collections per
job. It drives its own headless Blender, so the queue is unaffected by what you
do in the open one. **Save & Queue** takes the file you have open in Blender,
saves it, and adds it — the queue renders files on disk, so this is what stops
it rendering the version you saved an hour ago. Press it again after more work
and it re-uses the same row rather than stacking duplicates. **A render in
progress is written down as a resumable
job**: closing the app or losing power costs you the current frame and nothing
else. Live RAM and VRAM cards sit alongside (they stop sampling while you are
looking at another tab). Your machine is kept awake for the duration.

**Denoising setup** — one button builds the whole compositor tree.
The default mode gives **every light pass its own Denoise node** — Diffuse,
Glossy, Transmission and Volume, with Direct and Indirect denoised separately,
each guided by the denoising Normal and Albedo passes — then rebuilds the
beauty from the denoised parts and restores the layer's real alpha. Colour,
Emission and Environment are never denoised, because they are already clean.
A simpler one-node-per-view-layer mode is a tick away. Multiple view layers are
handled. **Remove Setup** puts your compositor back exactly as it was, from a
snapshot taken before the first run — and only ever deletes trees this tool
made. Cycles only; it says so plainly on EEVEE.

**Render presets** — save a look, apply it anywhere.
**164 settings across 15 groups**: engine and device, Cycles sampling,
denoising, light paths, EEVEE, film and motion blur, performance, resolution
and frame rate, output format, video encoding, colour management, simplify,
post processing. When you save one you tick which groups it keeps, with the
current values in front of you. **Output path and frame range start unticked**,
because those belong to a shot rather than to a look. Applying tells you what
changed, what already matched, and anything this Blender refused — and one
refused setting never stops the rest from landing. Presets are plain files you
can copy between machines, and **Save to Studio Library** files one away as a
library item you can tag, version and zip for a friend.

---

## Bone picker

An AnimSchool-style 2D picker that draws **inside Blender's own Image Editor**.

- Trace buttons over a reference picture; click one to select the bone under it.
- **Square buttons** select a bone, **round group buttons** select a whole set,
  **wide slider buttons** scrub a shape key right from the picker.
- Buttons live in canvas space, so they pan and zoom with the picture.
- Multiple tabs per rig — face, body, hands.
- **Buttons may overlap freely** — nothing blocks a move or a resize.
- **Scale one button on its own**: pick its row in the button list and use the
  Scale slider under it. The brush above works on the selection, which is what
  you want for many buttons at once and not what you want for a single group
  handle.
- **Bones & Extras** hides bones, empties and cameras in every 3D viewport in
  one press, so you can pick from the picture with the character clear of
  controls. Press again to bring them back.
- Layouts are stored **on the armature**, so they save inside the .blend and
  travel with the rig.
- Save a layout to the Studio Library and **retarget it onto another rig by
  bone name**.
- The app tab is the manager: tabs and rig, buttons, presets and appearance.
  Everything is drawn by Blender, so the two can never drift apart.

---

## Anim Layers

Animate in layers, on top of Blender's own NLA.

- Stack additive passes over a base animation; each layer has its own
  **influence** and **blend mode**.
- **Shape-key layers** as well as bone layers.
- **Set Keyframe / Remove Keyframe** buttons that do exactly what **I** and
  **Alt+I** do in the viewport, using your active keying set (or your
  Preferences default channels if you have none).
- **Merge / Bake** back down when you are happy, right under the layer list.
- There is a **panel inside Blender's N-panel** too, mirroring the same three
  settings, so you can work without switching windows.
- Layers cannot drift apart from Blender's: both sides read and write the same
  NLA tracks.

---

## Node Setup

Two node-editor helpers, driven from here rather than from a shelf inside
Blender.

- **Relink** — move a wired node's outgoing links onto an unconnected one, in
  any node tree. Sockets are matched by name and type; multi-input sockets keep
  both; there is a *copy inputs* option.
- **Image Sequence Setup** — point it at a compositor Image node and it counts
  the frames on disk, sets the sequence properties and the scene range, and
  builds the shot output path for you. No typing into file fields.

---

## Node Editor

A Blender-style node canvas for **texture baking**, driving Blender's own bake
operator.

**Six nodes**

- **Bake** — pick any material in your open scene from a searchable list (type
  any part of a name; Enter takes the best match). A **Bake all slots** tickbox
  bakes every material slot of that object in one run.
- **Bulk bake** — bake many meshes at once. *Selected to bake queue* takes
  everything selected in the viewport and queues every material slot of every
  mesh; anything that cannot bake (lights, cameras, meshes with no material or
  UVs) is ignored and counted. Or switch to folder mode and point it at a
  collection.
- **Collection** — every mesh inside a collection, all materials, all slots.
- **Map set** — tick the maps you want and one press bakes a whole PBR set,
  each saved as `<material>_<type>_baked`.
- **Bake settings** — Blender's Bake panel, option for option, in the panel's
  own order and with its own visibility rules: Type (all twelve — Combined,
  Ambient Occlusion, Shadow, Position, Normal, UV, Roughness, Emission,
  Environment, Diffuse, Glossy, Transmission), View From, the per-type
  Influence block, **Selected to Active** with the full cage family (Cage,
  Cage Object, Extrusion, Max Ray Distance), **Target** — a file or straight
  into the mesh's active Color Attribute — Clear Image, Margin with both
  margin types, resolution presets, and an optional sample override. Everything
  else (samples, denoiser, render device) comes from your scene, unchanged.
  This input takes **several wires**, so a Bake node and a Collection can drive
  one press together; a material both name is baked once.
- **Output image** — where maps land, and the result shown on the canvas.
  Leave the name empty and maps save as `<material>_baked` into the toolset's
  own `baked` folder. Two tickboxes: **Replace shader** wires the finished map
  back into the material it came from (your shader network stays exactly where
  it is, just unplugged, so one Ctrl+Z restores it), and **All slots** puts it
  into every material slot of the object. When a material has more than one
  Material Output the map goes to the one matching the render engine your
  baked material was using — and if the slot has no output for that engine,
  one is created.

**The canvas itself**

- Shift+A adds a node where your cursor is; Del/X removes selection.
- Wires are typed: a socket only connects to a socket of the same colour, and
  the status line says why a wire was refused.
- Ctrl-drag cuts wires. **Shift + right-drag adds reroutes** across every wire
  you cross, exactly like Blender.
- Middle-mouse pan, wheel and Ctrl +/− zoom, a proper dotted grid at every
  zoom level.
- A **?** on every node opens a plain-English panel explaining what it does.
- A progress bar under whichever node is working.
- Optional **remember node settings** (⚙ Library Settings) so nodes start with
  the values you last used.

Two things the bake does that Blender's own does not: a map that bakes empty
**tells you why** (no lights in the scene, fully transparent surface, or a
material with more than one Material Output), and a map whose bright areas are
clipping says so — with a note that naming the file `.exr` keeps the real
values.

---

## Optimization

Make a heavy scene fit in memory before you render it. **No original file is
ever modified**, and one click puts everything back.

- **Fixed size** — shrink chosen textures to a size you pick. Every run is
  remembered as a named **texture set**, so one scene can sit at one resolution
  and another at another; switch between them from the list. Queue several
  jobs with *Add to queue* and each becomes its own set, with its own objects
  and its own name (double-click a queue row to rename it).
- **Adaptive** — the interesting one. Each texture is shrunk to the size it
  actually needs, **measured from how large its object lands in the render
  camera**. A wall at the back of the shot does not need a 4K map.
- **Meshes** — a managed Decimate on distant meshes, ratio driven by camera
  distance.
- **Restore** — put every original back. Survives a *Save As*, warns in red
  about a texture it genuinely cannot find, and has a **Clear cache folder**
  button that restores first, then deletes only the files the Toolset wrote.
- **Memory report** — what each datablock costs, plus a **VRAM estimate**:
  roughly what a render needs from the command line versus from inside Blender,
  and what the difference is made of.
- Runs off the interface thread with a real progress bar counting actual
  textures, and the cache is watched — if a set's files are cleared or moved
  you are told before you switch to it.

---

## NSFW Tools

Ready-made MADI rigs, built into your scene in one click.

**Penetration Tech** — a torus whose geometry-node rig dents and bulges
wherever a mesh in its `Affectors` collection passes through it. It arrives as
an ordinary, fully visible Geometry Nodes modifier: point its Affectors input
at a collection of your own, tune it on the modifier, and bind it to your own
mesh with a Surface Deform to drive that too.

---

## Physics

**Bone Jiggle** — spring-driven motion on **bones**: hair, tails, ears, chains,
cloth trim. Each bone gets a simulated point pulled toward the pose the
animation asks for, and the bone is aimed at where the point ended up.

- Per-end settings (tip and root) on their own sub-tabs, so a chain can be
  loose at the end and stiff at the base without a wall of controls.
- Dynamics: stiffness, damping, mass, gravity, stretch.
- **Collision** — analytic colliders (sphere, capsule, plane, box), real mesh
  colliders, a whole collection of them, plus friction and bounce, and **self
  collision** within a chain.
- **Wind** and Blender's force fields — all three types.
- **Lateral links** between neighbouring chains so a fringe moves as one.
- **Bake** to keyframes over a frame range with preroll, and a motion cache so
  scrubbing is not a re-simulation.
- Copy settings between bones; list and select what is already jiggling.

---

## What's New

This page. It ships inside the build, so it is readable with no internet and no
licence — which is the one moment release notes are worth anything.

---

## Across the whole app

- **It updates itself.** Check from the status bar. A release downloads only
  the files that changed, verifies every byte against a signature, proves the
  new build actually starts, and puts the old one back if it does not.
  **Updating is free for everyone**, and so is every tab — nobody should be
  stuck on an old build because of what they have or have not bought.
- **Your key is for packs now.** Every tool in the app is free. Supporting on
  Patreon earns a key that will unlock every premium pose and animation pack
  when the MADI Packs store arrives — one key, all packs, for a year at a
  time. Nothing you have saved is ever affected by a key running out.
- **⚙ Settings on every tab** — library folder, theme, and the per-tool options.
- **ⓘ About** — the app version, the Blender add-on version actually connected,
  and links to **Discord** (report a bug) and **Patreon**.
- **Super focus** (status bar, off by default) — while it is on, whichever of
  this app or Blender your mouse is over takes focus, so clicking a button here
  never costs an extra click just to arrive. Nothing else on your desktop is
  touched.
- **Always-on-top pin**, so the app can sit over Blender.
- **Only one copy runs at a time.** Launching it again — from the shortcut or
  from Blender's Open Toolset App button — brings the window you already have
  to the front rather than opening a second one onto the same library.
- **A developer console** with the app's own log, for when something needs
  reporting.
- **The mouse wheel scrolls and never changes a setting** under the cursor —
  application-wide, every tab, every control type.
- **Every numeric setting is a drag slider**: click-drag to change, Shift for
  fine, double-click to type.
- **Nothing you save lives inside the app folder** except by choice, so an
  update never touches your library, your render queue, your presets or your
  baked maps.

---

*The build you are running is named at the top of this tab, and the add-on
version actually connected is under ⓘ in the status bar — along with the
Discord link for reporting anything odd.*
