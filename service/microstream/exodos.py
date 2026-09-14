"""eXoDOS titles, read straight from the collection.

eXoDOS keeps each game as `eXo/eXoDOS/<Title (Year)>.zip`, one top-level folder
inside, and the way it launches that game in `eXo/eXoDOS/!dos/<folder>/
dosbox.conf`. From those two -- the zip's file list, never its contents -- this
works out what DOSBox has to do: which disc or floppy images to mount, which
drive and folder to start from, which command to run.

Two ways to use the answer:

* In place: js-dos gets a tiny generated zip holding the folder tree and a
  dosbox.conf, followed by the eXoDOS zip itself, loaded from wherever the
  collection lives. Nothing is copied or extracted to disk, so a 500 MB CD
  title costs no space beyond what the collection already takes.
* As a bundle (server/jsdos/from-exodos.py): the game folder becomes the root
  of a self-contained .jsdos file.
"""

import io
import os
import re
import zipfile

#: DOSBox settings for the cabinet. The picture is scaled to 128x128 anyway, so
#: resolution does not matter, but CPU does: a 3D game starved of cycles is the
#: difference between flying and a slideshow.
BASE_CONF = """[sdl]
autolock=false

[cpu]
core=auto
cputype=auto
cycles=max

[render]
aspect=false
"""

#: A token on a batch line: a quoted path with spaces, or a plain word.
TOKEN = re.compile(r'"[^"]*"|\S+')

#: Batch lines that are not the game's own command.
SKIP = ("mount", "imgmount", "cls", "exit", "pause", "echo", "rem", "[")


def _quote(path):
    return '"%s"' % path if " " in path else path


class Collection:
    def __init__(self, root):
        self.root = root or ""
        self.games_dir = os.path.join(self.root, "eXo", "eXoDOS")
        self.dos_dir = os.path.join(self.games_dir, "!dos")

    def exists(self):
        return bool(self.root) and os.path.isdir(self.games_dir)

    def zip_path(self, title):
        return os.path.join(self.games_dir, title + ".zip")

    def has(self, title):
        return self.exists() and os.path.exists(self.zip_path(title))

    def search(self, pattern):
        pat = pattern.lower()
        return sorted(f[:-4] for f in os.listdir(self.games_dir)
                      if f.lower().endswith(".zip") and pat in f.lower())

    def title(self, title):
        return Title(self, title)


class Title:
    """One game: what its zip holds, and how eXoDOS starts it."""

    def __init__(self, collection, title):
        self.title = title
        self.zip_path = collection.zip_path(title)
        if not os.path.exists(self.zip_path):
            raise RuntimeError("no such eXoDOS title: %s" % self.zip_path)
        self.size = os.path.getsize(self.zip_path)
        with zipfile.ZipFile(self.zip_path) as z:
            self.names = [n for n in z.namelist() if n]
        self.top = self.names[0].split("/")[0]

        # Every file and folder, keyed by lower case. Paths in a DOS batch file
        # never cared about case; js-dos's file system does.
        self._real = {}
        self.dirs = set()
        for n in self.names:
            parts = n.rstrip("/").split("/")
            for i in range(1, len(parts) + 1):
                path = "/".join(parts[:i])
                self._real.setdefault(path.lower(), path)
                if i < len(parts) or n.endswith("/"):
                    self.dirs.add(path)

        self.conf_path = os.path.join(collection.dos_dir, self.top, "dosbox.conf")
        self.cmd, self.subdir, self.mounts, self.drive = self._parse()

    # --- the zip's file list ----------------------------------------------

    def real(self, path):
        path = path.strip("/")
        return self._real.get(path.lower(), path)

    def is_dir(self, path):
        return self.real(path) in self.dirs

    def _files_in(self, folder):
        prefix = self.real(folder).lower() + "/"
        found = {}
        for low, real in self._real.items():
            rest = low[len(prefix):]
            if low.startswith(prefix) and rest and "/" not in rest:
                found[rest] = real.split("/")[-1]
        return found

    # --- the launch script ------------------------------------------------

    def _autoexec(self):
        if not os.path.exists(self.conf_path):
            return []
        with open(self.conf_path, "r", errors="replace") as fh:
            text = fh.read()
        idx = text.lower().find("[autoexec]")
        if idx < 0:
            return []
        lines = []
        for raw in text[idx:].splitlines()[1:]:
            line = raw.strip().lstrip("@").strip()
            if line.lower().startswith("exit"):
                break
            if line:
                lines.append(line)
        return lines

    def _parse(self):
        """(command, subfolder, extra mounts, start drive) as eXoDOS runs it.

        The subfolder matters: several titles keep the game one level down and
        are started with `cd viz` first. Mounts besides C: are CD and floppy
        images; a few titles then switch to that drive before running anything.
        """
        cmd = subdir = None
        mounts, drive = [], "c:"
        prefix = re.compile(r"^\.?[\\/]?exodos[\\/]" + re.escape(self.top) + r"(?:[\\/]|$)",
                            re.I)

        for line in self._autoexec():
            low = line.lower()
            if re.match(r"^[a-z]:$", low):
                drive = low
                continue

            tokens = [t.strip('"') for t in TOKEN.findall(line)]
            if tokens[0].lower() in ("mount", "imgmount") and len(tokens) >= 3:
                if tokens[1].lower() == "c":
                    continue          # the game folder is always C:
                paths, rest = [], []
                for tok in tokens[2:]:
                    if rest or tok.startswith("-"):
                        rest.append(tok)
                    else:
                        rel = prefix.sub("", tok.replace("/", "\\"))
                        paths.append(rel.replace("\\", "/").strip("/"))
                mounts.append((tokens[0].lower(), tokens[1], paths, rest))
                continue

            if cmd is not None:
                continue
            if low.startswith("cd "):
                target = line[3:].strip().strip('"').replace("\\", "/").strip("/")
                if target.startswith("./"):
                    target = target[2:]
                # `cd ..` only walks back out to the mount point.
                if target and target != "..":
                    subdir = target
                continue
            if low.startswith(SKIP):
                continue
            if low.startswith("call "):
                line = line[5:].strip()
            cmd = line

        if subdir and not self.is_dir(self.top + "/" + subdir):
            subdir = None

        if cmd and drive == "c:":
            cmd = self._with_extension(cmd, self.top + ("/" + subdir if subdir else ""))
        if not cmd:
            cmd, subdir = self._guess_command()
        return cmd, subdir, mounts, drive

    def _with_extension(self, cmd, folder):
        """`call run` means RUN.BAT or RUN.EXE -- whichever is actually there.
        On another drive (a CD) the files are not in the zip to check, so a
        bare name is left for DOS to find."""
        name, _, args = cmd.partition(" ")
        if re.search(r"\.(exe|com|bat)$", name, re.I):
            return cmd
        files = self._files_in(folder)
        for ext in (".bat", ".exe", ".com"):
            if (name + ext).lower() in files:
                name = files[(name + ext).lower()]
                break
        else:
            name += ".EXE"
        return (name + " " + args).strip()

    def _guess_command(self):
        base = self.top.lower() + "/"
        runnable = sorted((r for low, r in self._real.items()
                           if low.startswith(base) and re.search(r"\.(exe|com|bat)$", low)),
                          key=len)
        if not runnable:
            return None, None
        rel = runnable[0][len(self.top) + 1:]
        if "/" in rel:
            folder, name = rel.rsplit("/", 1)
            return name, folder
        return rel, None

    # --- what DOSBox is told ----------------------------------------------

    def _host_path(self, rel, in_place):
        full = self.real(self.top + ("/" + rel if rel else ""))
        rel_real = full[len(self.top):].strip("/")
        if in_place:
            return "/".join(p for p in (self.top, rel_real) if p)
        return rel_real or "."

    def autoexec(self, in_place):
        """The [autoexec] lines. In place, the zip's top folder is C:; in a
        bundle, the game folder is the root."""
        lines = ["mount c %s" % _quote(self.top if in_place else ".")]
        for verb, letter, paths, rest in self.mounts:
            lines.append(" ".join([verb, letter] +
                                  [_quote(self._host_path(p, in_place)) for p in paths] +
                                  rest))
        lines.append(self.drive)
        if self.subdir:
            lines.append("cd " + self.subdir.replace("/", "\\"))
        if self.cmd:
            lines.append(self.cmd)
        return lines

    def dosbox_conf(self, in_place=True):
        return BASE_CONF + "\n[autoexec]\n" + "\n".join(self.autoexec(in_place)) + "\n"

    def skeleton(self):
        """The zip js-dos loads before the eXoDOS one: every folder of the game,
        created up front, and the dosbox.conf.

        eXoDOS zips carry no folder entries, and js-dos creates a file's parent
        folder but not its grandparent -- so without this, a game kept two
        levels deep never starts."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for folder in sorted(self.dirs, key=lambda p: (p.count("/"), p)):
                z.writestr(folder + "/", b"")
            z.writestr(".jsdos/dosbox.conf", self.dosbox_conf(in_place=True))
        return buf.getvalue()
