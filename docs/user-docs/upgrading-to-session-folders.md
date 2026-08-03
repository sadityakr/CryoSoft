# Upgrading to Session folders

CryoSoft now organizes experiments inside **Sessions** — named folders that
each hold several experiments. If you used CryoSoft before this change,
your existing experiment folders sit directly under the measurement root
(the fixed data folder your installation is configured to use), rather than
inside a Session folder. This note explains what changed and how to see
your old experiments again if you want to.

## What you'll notice

The **Open Experiment…** dialog (User menu → `Load Session…`) now only
lists experiments that live inside the currently active Session. Old
experiment folders from before this update are not shown there anymore —
they haven't moved or been deleted, they're just outside the folder the
dialog looks in.

## Nothing is lost, and nothing moves automatically

Your old data is untouched, exactly where it always was. CryoSoft does not
move anything on your behalf: whether to bring old experiments into a
Session is entirely your choice, and skipping this is fine if you don't
need to reopen that data through the app.

## How to make an old experiment visible again

If you want an old experiment to show up in Open Experiment… again, move
its folder into a Session folder by hand:

1. Pick or create the Session you want it to belong to. Use User menu →
   `Resume Session…` → `New session…` if you don't have one yet.
2. Find the experiment's folder. It currently sits directly under the
   measurement root, e.g. `<measurement_root>/<experiment_id>/`.
3. Move that whole folder into the Session's folder, so it ends up at
   `<measurement_root>/sessions/<session_id>/<experiment_id>/`.

Use Windows Explorer or any plain file move for this — there is no button
in CryoSoft that does it for you. Once moved, the experiment appears in
Open Experiment… the next time that Session is active.

## Ask if you're not sure

If you're not sure which Session an old experiment should go into, or
you're unsure where your measurement root is, ask before moving anything —
moving the wrong folder into the wrong Session is easy to undo (just move
it back), but it's still better to check first.
