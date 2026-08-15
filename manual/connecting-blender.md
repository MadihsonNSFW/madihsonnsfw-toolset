# Connecting Blender

The app and Blender talk over a TCP connection on **port 9877**, bound to your
own machine. Nothing leaves it.

## Starting the bridge

Blender's 3D viewport ▸ **N** ▸ **MADI** ▸ **Start bridge**.

The bridge does **not** start itself. You start it, once per Blender, on purpose
— which is what stops a second Blender quietly taking the connection out from
under the file you are working in.

The panel gives you three things:

- **Start / Stop bridge**
- **Open Toolset App** — the first time you press it, Blender asks you to point
  at the exe and remembers where it is. Nothing about where you keep the app is
  assumed.
- **Watch last render**

## Reading the status bar

The app's status bar names what it is connected to, including the open .blend.
If that filename is not the one you think you are working on, stop and look
before you apply anything.

---

## More than one Blender

**One port, one holder.** If Blender A has the bridge, Blender B cannot take it;
B will say so rather than stealing it.

!!! warning "Installing the add-on frees the port"
    Installing or updating the extension makes Blender reload it, and a reload
    stops the bridge. If a second Blender is open and waiting, it can grab the
    free port in that gap — so the instance you were pushing to goes quiet and a
    *different* one answers. The app reads the result off disk rather than
    trusting whoever answers the port, but the symptom to recognise is: you
    pressed **Update add-on**, and afterwards the connected Blender is not the
    one you were looking at.

    If that happens: stop the bridge in the Blender that grabbed it, then start
    it in the one you want.

Remember also that **extensions install per Blender version**. Updating the
add-on while connected to 5.1 leaves 5.2 on whatever it had.

---

## Troubleshooting

**The app says it is not connected.**

1. Is the bridge started in Blender's N-panel?
2. Is another Blender holding it?
3. Restart the bridge — stop, then start.

**A tool says it needs a newer add-on.**

Press **⚙ Settings ▸ Update add-on**. The app carries the exact add-on version
it expects, so this always resolves the mismatch. Individual tools declare what
they need, so an older add-on costs you that one tool rather than the whole app.

**A push seemed to succeed but nothing changed.**

Check the add-on version reported under **ⓘ About** in the status bar — that is
the version actually connected, read from the running extension rather than from
what the app hoped it installed.

**Blender is running but the app never finds it.**

The bridge binds to the loopback interface only. A VPN or a firewall rule that
intercepts loopback traffic will break it. Nothing about the connection reaches
the network.
