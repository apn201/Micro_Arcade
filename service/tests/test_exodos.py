#!/usr/bin/env python3
"""Reading eXoDOS titles, against a small fake collection built here.

No real collection needed. Each fake title reproduces something an actual
eXoDOS launch script did that broke a game: a CD image in a folder whose case
differs from the script, a path with spaces, `pause` before the command, a
drive switch, `call run` with no extension, and a zip with no folder entries.

    python service/tests/test_exodos.py
"""

import io
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from microstream.exodos import Collection


def make_title(root, title, top, files, autoexec):
    games = os.path.join(root, "eXo", "eXoDOS")
    os.makedirs(os.path.join(games, "!dos", top), exist_ok=True)
    # Like eXoDOS: files only, no folder entries.
    with zipfile.ZipFile(os.path.join(games, title + ".zip"), "w") as z:
        for name in files:
            z.writestr(top + "/" + name, b"x")
    with open(os.path.join(games, "!dos", top, "dosbox.conf"), "w") as fh:
        fh.write("[sdl]\nfullscreen=true\n\n[autoexec]\n" + "\n".join(autoexec) + "\n")


def test_exodos():
    with tempfile.TemporaryDirectory() as root:
        make_title(root, "Crime Scene (1994)", "CrimeScn",
                   ["CD/CRIME.ISO", "CS/RUN.BAT", "CS/CS.EXE"],
                   ["cd ..", "cd ..", r"mount c .\eXoDOS\CrimeScn",
                    r"imgmount d .\eXoDOS\CrimeScn\cd\crime.iso -t cdrom",
                    "c:", "@cd CS", "@cls", "@call run", "exit"])
        make_title(root, "Brain Test (1995)", "BrainTst",
                   ["cd/Brain Test (USA).cue", "cd/Brain Test (USA).bin"],
                   [r"mount c .\eXoDOS\BrainTst",
                    r'imgmount d ".\eXoDOS\BrainTst\cd\Brain Test (USA).cue" -t cdrom',
                    "d:", "@cls", "@brain", "exit"])
        make_title(root, "Deep Game (1992)", "Deep",
                   ["Deep/deep/DEEP.EXE", "HELP/README.TXT"],
                   [r"mount c .\eXoDOS\Deep", "c:", "echo Press 0 to start", "pause",
                    r"@cd .\Deep\deep", "@deep", "exit"])

        exo = Collection(root)
        assert exo.exists() and exo.has("Crime Scene (1994)")
        assert not Collection("").exists()

        crime = exo.title("Crime Scene (1994)")
        # CD image found despite the script's lower-case folder; `call run`
        # resolved against the real files; the folder that holds them kept.
        assert crime.autoexec(in_place=False) == [
            "mount c .", "imgmount d CD/CRIME.ISO -t cdrom", "c:", "cd CS", "RUN.BAT"
        ], crime.autoexec(in_place=False)
        assert crime.autoexec(in_place=True) == [
            "mount c CrimeScn", "imgmount d CrimeScn/CD/CRIME.ISO -t cdrom", "c:",
            "cd CS", "RUN.BAT"
        ], crime.autoexec(in_place=True)

        brain = exo.title("Brain Test (1995)")
        # Quoted path with spaces survives, and the game starts from D:
        # with its bare name left for DOS to find on the disc.
        assert brain.autoexec(in_place=True) == [
            "mount c BrainTst", 'imgmount d "BrainTst/cd/Brain Test (USA).cue" -t cdrom',
            "d:", "brain"
        ], brain.autoexec(in_place=True)

        deep = exo.title("Deep Game (1992)")
        # `pause` is not the game, and a subfolder two levels down is kept.
        assert deep.cmd == "DEEP.EXE" and deep.subdir == "Deep/deep", (deep.cmd, deep.subdir)
        assert deep.autoexec(in_place=True)[-2:] == ["cd Deep\\deep", "DEEP.EXE"]

        # The skeleton creates every folder before any file arrives, and
        # carries the dosbox.conf js-dos boots from.
        with zipfile.ZipFile(io.BytesIO(deep.skeleton())) as z:
            names = z.namelist()
            conf = z.read(".jsdos/dosbox.conf").decode()
        folders = [n for n in names if n.endswith("/")]
        assert set(folders) == {"Deep/", "Deep/HELP/", "Deep/Deep/", "Deep/Deep/deep/"}, names
        for i, folder in enumerate(folders):         # every parent before its children
            parent = folder.rstrip("/").rpartition("/")[0]
            assert not parent or parent + "/" in folders[:i], (folder, folders)
        assert "cycles=max" in conf and conf.rstrip().endswith("DEEP.EXE"), conf

    print("PASS  exodos: case, spaces, pause, drive switch, call run, skeleton folders")


if __name__ == "__main__":
    test_exodos()
