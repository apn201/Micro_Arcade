// Minimal de-risking probe: does js-dos run headless in Node and hand us
// frames? Writes the newest frame as raw RGB so it can be inspected.
//
//   node probe.js digger.jsdos out.rgb
const fs = require("fs");
const path = require("path");

require("emulators");
const emulators = global.emulators;
// Where wdosbox.js / .wasm live.
emulators.pathPrefix = path.join(__dirname, "node_modules", "emulators", "dist") + path.sep;

const bundlePath = process.argv[2] || "digger.jsdos";
const outPath = process.argv[3] || "out.rgb";

// --keys 3500:49,6500:257   scripted keypresses: "millisecond:keycode"
// --until 20000             how long to run before writing the frame
// --shots 6000,12000        extra frames, written as out.rgb.6000 etc.
function argOf(name) {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : null;
}
// "ms:code" taps the key; "ms:code:hold" holds it for that many ms, which is
// how you tell a game that responds to input from a menu that does not.
const scripted = (argOf("--keys") || "").split(",").filter(Boolean).map((s) => {
  const [at, code, hold] = s.split(":");
  return {
    at: parseInt(at, 10),
    code: parseInt(code, 10),
    hold: hold ? parseInt(hold, 10) : 90,
  };
});
const runFor = parseInt(argOf("--until") || "6000", 10);
const shots = (argOf("--shots") || "").split(",").filter(Boolean).map(Number);

// --before FILE loads a zip ahead of the bundle (repeatable). That is how a
// title runs in place from eXoDOS: a generated zip of folders and dosbox.conf,
// then the collection's own zip.
const before = [];
for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i] === "--before" && process.argv[i + 1]) before.push(process.argv[++i]);
}
const init = before.concat([bundlePath]).map((f) => new Uint8Array(fs.readFileSync(f)));
console.log("bundle:", bundlePath, init[init.length - 1].length, "bytes",
            before.length ? "(after " + before.length + " more)" : "");
console.log("pathPrefix:", emulators.pathPrefix);
console.log("backends:", Object.keys(emulators).filter((k) => typeof emulators[k] === "function").join(", "));

let frames = 0;
let last = null;
let w = 0;
let h = 0;
let channels = 0;

emulators.dosboxNode(init.length === 1 ? init[0] : init).then(async (ci) => {
  console.log("command interface up");

  ci.events().onFrameSize((fw, fh) => {
    w = fw;
    h = fh;
    console.log("frame size:", fw, "x", fh);
  });

  ci.events().onFrame((rgb, rgba) => {
    frames++;
    const buf = rgb || rgba;
    if (buf) {
      last = buf;
      channels = rgb ? 3 : 4;
    }
  });

  ci.events().onStdout((msg) => process.stdout.write("dos: " + msg));

  // Play the scripted keys. Most DOS games open with a setup question and a
  // kiosk has no keyboard, so this is how a title gets to its first frame of
  // actual gameplay unattended.
  for (const k of scripted) {
    setTimeout(() => {
      try {
        ci.sendKeyEvent(k.code, true);
        setTimeout(() => ci.sendKeyEvent(k.code, false), k.hold);
      } catch (e) {
        console.error("key " + k.code + " failed: " + e.message);
      }
    }, k.at);
  }

  for (const at of shots) {
    setTimeout(() => {
      if (last) {
        fs.writeFileSync(outPath + "." + at, Buffer.from(last));
        console.log("shot " + at + " " + w + "x" + h);
      }
    }, at);
  }

  setTimeout(async () => {
    console.log("frames received:", frames);
    if (last) {
      console.log("frame bytes:", last.length, "channels:", channels,
                  "expected:", w * h * channels);
      fs.writeFileSync(outPath, Buffer.from(last));
      console.log("wrote", outPath, "(" + w + "x" + h + ", " + channels + " channels)");
    } else {
      console.log("NO FRAMES");
    }
    // Tearing down a wasm emulator is best-effort and some titles never
    // resolve it, so never wait on it: the frame is already written.
    try {
      ci.exit();
    } catch (e) {
      // ignore
    }
    setTimeout(() => process.exit(last ? 0 : 1), 300);
  }, runFor);
}).catch((e) => {
  console.error("failed:", e && e.stack ? e.stack : e);
  process.exit(2);
});
