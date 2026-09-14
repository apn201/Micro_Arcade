// Build a .jsdos bundle from a folder of DOS game files.
//
//   node make-bundle.js <game-folder> <output.jsdos> [start command]
//   node make-bundle.js ./descent descent.jsdos "DESCENT.EXE"
//
// A .jsdos bundle is just a zip: the game's files at the root, plus a
// `.jsdos/dosbox.conf` telling DOSBox what to mount and run. That means you
// never need a CDN or anyone else's packaging -- point this at your own copy
// of a game and it becomes a kiosk entry.
//
// If no start command is given, the first .EXE, .COM or .BAT found is used.

const fs = require("fs");
const path = require("path");
const zlib = require("zlib");

const folder = process.argv[2];
const outPath = process.argv[3];
let startCmd = process.argv[4] || null;

// Some games live in a subfolder of the archive and expect to be started from
// there (eXoDOS does `cd viz` before `call run`). Running them from the root
// gets "Illegal command" or missing data files.
let startDir = null;
const cdIndex = process.argv.indexOf("--cd");
if (cdIndex > 0 && process.argv[cdIndex + 1]) {
  startDir = process.argv[cdIndex + 1];
  if (startCmd === "--cd") startCmd = process.argv[cdIndex + 2] || null;
}

// Mounts besides C: that the game expects -- a CD image, a floppy -- and the
// drive to start from when that is not C:. Both come from eXoDOS's own
// dosbox.conf via from-exodos.py:
//   --pre "imgmount d cd/game.cue -t cdrom"   (repeatable)
//   --drive d:
const preLines = [];
let startDrive = "c:";
for (let i = 4; i < process.argv.length; i++) {
  if (process.argv[i] === "--pre" && process.argv[i + 1] !== undefined) {
    preLines.push(process.argv[++i]);
  } else if (process.argv[i] === "--drive" && process.argv[i + 1]) {
    startDrive = process.argv[++i];
  }
}
if (startCmd && startCmd.startsWith("--")) startCmd = null;

if (!folder || !outPath) {
  console.error("usage: make-bundle.js <game-folder> <output.jsdos> [START.EXE]");
  process.exit(2);
}
if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) {
  console.error("not a folder: " + folder);
  process.exit(2);
}

// --- collect files --------------------------------------------------------

const files = [];
(function walk(dir, prefix) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    // Any existing .jsdos/ is regenerated below; keeping the old one would
    // put two dosbox.conf entries in the archive.
    if (entry.name === ".jsdos") continue;
    const full = path.join(dir, entry.name);
    const rel = prefix ? prefix + "/" + entry.name : entry.name;
    if (entry.isDirectory()) walk(full, rel);
    else files.push({ rel, full, size: fs.statSync(full).size });
  }
})(folder, "");

if (!files.length) {
  console.error("no files in " + folder);
  process.exit(2);
}

if (!startCmd) {
  const runnable = files
    .map((f) => f.rel)
    .filter((r) => /\.(exe|com|bat)$/i.test(r))
    .sort((a, b) => a.length - b.length);
  if (!runnable.length) {
    console.error("no .EXE/.COM/.BAT found; pass the start command explicitly");
    process.exit(2);
  }
  startCmd = runnable[0].replace(/\//g, "\\");
  console.error("start command not given, using: " + startCmd);
}

// DOSBox settings that matter for this cabinet: the output is scaled to
// 128x128 anyway, so resolution is irrelevant, but cycles are not -- a 3D
// game starved of CPU is the difference between "flies" and "slideshow".
const conf = [
  "[sdl]",
  "autolock=false",
  "",
  "[cpu]",
  "core=auto",
  "cputype=auto",
  "cycles=max",
  "",
  "[render]",
  "aspect=false",
  "",
  "[autoexec]",
  "mount c .",
  ...preLines,
  startDrive,
  ...(startDir ? ["cd " + startDir] : []),
  startCmd,
  "",
].join("\n");

files.push({ rel: ".jsdos/dosbox.conf", data: Buffer.from(conf, "ascii") });

// Explicit directory entries. The backend creates a file's immediate parent
// but not its grandparent, so a title kept two levels down (WOLF3D/WOLF3D/...)
// dies on extraction with "No such file or directory". Every zip writer worth
// the name emits these; ours has to as well.
(function addDirs() {
  const seen = new Set();
  const dirs = [];
  for (const f of files) {
    const parts = f.rel.split("/");
    for (let i = 1; i < parts.length; i++) {
      const dir = parts.slice(0, i).join("/") + "/";
      if (!seen.has(dir)) {
        seen.add(dir);
        dirs.push({ rel: dir, data: Buffer.alloc(0), dir: true });
      }
    }
  }
  // Parents before children, and all of them before any file.
  dirs.sort((a, b) => a.rel.localeCompare(b.rel));
  files.unshift(...dirs);
})();

// --- write the zip --------------------------------------------------------
// Written by hand rather than pulling in a zip dependency: the format is
// small, and this keeps the project's dependency list at "emulators".

function crc32(buf) {
  let c;
  const table = crc32.table || (crc32.table = (() => {
    const t = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c;
    }
    return t;
  })());
  let crc = -1;
  for (let i = 0; i < buf.length; i++) crc = (crc >>> 8) ^ table[(crc ^ buf[i]) & 0xff];
  return (crc ^ -1) >>> 0;
}

const chunks = [];
const central = [];
let offset = 0;

for (const f of files) {
  const name = Buffer.from(f.rel, "ascii");
  const raw = f.data || fs.readFileSync(f.full);
  const deflated = f.dir ? raw : zlib.deflateRawSync(raw, { level: 9 });
  const useDeflate = !f.dir && deflated.length < raw.length;
  const body = useDeflate ? deflated : raw;
  const method = useDeflate ? 8 : 0;
  const crc = crc32(raw);

  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(20, 4);            // version needed
  local.writeUInt16LE(0, 6);             // flags
  local.writeUInt16LE(method, 8);
  local.writeUInt16LE(0, 10);            // time
  local.writeUInt16LE(0x21, 12);         // date (1980-01-01)
  local.writeUInt32LE(crc, 14);
  local.writeUInt32LE(body.length, 18);
  local.writeUInt32LE(raw.length, 22);
  local.writeUInt16LE(name.length, 26);
  local.writeUInt16LE(0, 28);

  chunks.push(local, name, body);

  const cen = Buffer.alloc(46);
  cen.writeUInt32LE(0x02014b50, 0);
  cen.writeUInt16LE(20, 4);
  cen.writeUInt16LE(20, 6);
  cen.writeUInt16LE(0, 8);
  cen.writeUInt16LE(method, 10);
  cen.writeUInt16LE(0, 12);
  cen.writeUInt16LE(0x21, 14);
  cen.writeUInt32LE(crc, 16);
  cen.writeUInt32LE(body.length, 20);
  cen.writeUInt32LE(raw.length, 24);
  cen.writeUInt16LE(name.length, 28);
  cen.writeUInt16LE(0, 30);
  cen.writeUInt16LE(0, 32);
  cen.writeUInt16LE(0, 34);
  cen.writeUInt16LE(0, 36);
  cen.writeUInt32LE(f.dir ? 0x10 : 0, 38);   // MS-DOS directory attribute
  cen.writeUInt32LE(offset, 42);
  central.push(cen, name);

  offset += local.length + name.length + body.length;
}

const centralBuf = Buffer.concat(central);
const end = Buffer.alloc(22);
end.writeUInt32LE(0x06054b50, 0);
end.writeUInt16LE(0, 4);
end.writeUInt16LE(0, 6);
end.writeUInt16LE(files.length, 8);
end.writeUInt16LE(files.length, 10);
end.writeUInt32LE(centralBuf.length, 12);
end.writeUInt32LE(offset, 16);
end.writeUInt16LE(0, 20);

fs.writeFileSync(outPath, Buffer.concat([...chunks, centralBuf, end]));

console.log("wrote " + outPath + " (" + files.length + " files, " +
            fs.statSync(outPath).size + " bytes), starts " + startCmd);
console.log("try it:  python service/run.py --source jsdos --bundle " + outPath);
