// js-dos (DOSBox) as a frame source for the micro-doom streaming service.
//
// Same contract as the native DOOM backend in server/src/doomgeneric_pipe.c:
// connect back over loopback TCP, ship raw frames, accept key events. The
// service does the scaling, encoding, transport and input mapping, so this
// file is deliberately thin -- see docs/SOURCE_PROTOCOL.md.
//
//   node jsdos-source.js --connect 127.0.0.1:5555 --bundle digger.jsdos
//
// Not meant to be run by hand; the service spawns it.

const fs = require("fs");
const net = require("net");
const path = require("path");

require("emulators");
const emulators = global.emulators;

// --- arguments -----------------------------------------------------------

const args = process.argv.slice(2);
let host = "127.0.0.1";
let port = 0;
let bundlePath = null;
let backend = "dosboxNode";     // dosboxXNode also exists (DOSBox-X)

for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === "--connect") {
    const [h, p] = String(args[++i]).split(":");
    host = h;
    port = parseInt(p, 10);
  } else if (a === "--bundle") {
    bundlePath = args[++i];
  } else if (a === "--backend") {
    backend = args[++i];
  } else if (a === "--help") {
    console.error("usage: jsdos-source.js --connect HOST:PORT --bundle FILE.jsdos");
    process.exit(0);
  }
}

if (!port || !bundlePath) {
  console.error("jsdos: --connect HOST:PORT and --bundle FILE are required");
  process.exit(2);
}

emulators.pathPrefix = path.join(__dirname, "node_modules", "emulators", "dist") + path.sep;

// --- source protocol -----------------------------------------------------

const FRAME_MAGIC = Buffer.from("DGF1", "ascii");
const HDR_LEN = 16;
const MSG_KEY = 1;        // [type][pressed][key]          native 8-bit codes
const MSG_QUIT = 2;       // [type][0][0]
const MSG_KEY16 = 3;      // [type][pressed][lo][hi]       js-dos 16-bit codes

const STATE_LEVEL = 0;

let header = Buffer.alloc(HDR_LEN);
FRAME_MAGIC.copy(header, 0);

let ci = null;
let socketWritable = true;
let framesIn = 0;
let framesSent = 0;
let framesDropped = 0;

const sock = net.connect(port, host, () => {
  sock.setNoDelay(true);
  console.error("jsdos: connected to " + host + ":" + port);
  start();
});

sock.on("drain", () => {
  socketWritable = true;
});

sock.on("error", (e) => {
  console.error("jsdos: socket error: " + e.message);
  shutdown(0);
});

sock.on("close", () => {
  console.error("jsdos: service closed the connection");
  shutdown(0);
});

// Input arrives as a byte stream, so it needs real framing rather than
// assuming one message per packet.
let inbox = Buffer.alloc(0);

sock.on("data", (chunk) => {
  inbox = inbox.length ? Buffer.concat([inbox, chunk]) : chunk;

  for (;;) {
    if (inbox.length < 1) return;
    const type = inbox[0];
    const need = type === MSG_KEY16 ? 4 : 3;
    if (inbox.length < need) return;

    const msg = inbox.subarray(0, need);
    inbox = inbox.subarray(need);

    if (type === MSG_QUIT) {
      console.error("jsdos: quit requested");
      shutdown(0);
      return;
    }
    if (!ci) continue;

    try {
      if (type === MSG_KEY16) {
        ci.sendKeyEvent(msg[2] | (msg[3] << 8), msg[1] !== 0);
      } else if (type === MSG_KEY) {
        ci.sendKeyEvent(msg[2], msg[1] !== 0);
      }
    } catch (e) {
      console.error("jsdos: sendKeyEvent failed: " + e.message);
    }
  }
});

// --- emulator ------------------------------------------------------------

function sendFrame(buf, width, height, channels) {
  // Drop frames while the socket is backed up. The service only ever wants
  // the newest frame, so queueing stale ones would add latency and memory
  // for no benefit.
  if (!socketWritable) {
    framesDropped++;
    return;
  }

  header.writeUInt16LE(width, 4);
  header.writeUInt16LE(height, 6);
  header.writeUInt8(channels, 8);
  header.writeUInt8(STATE_LEVEL, 9);
  header.writeUInt16LE(0, 10);
  header.writeUInt32LE(buf.length, 12);

  socketWritable = sock.write(header) && sock.write(Buffer.from(buf.buffer, buf.byteOffset, buf.length));
  framesSent++;
}

function start() {
  const bundle = new Uint8Array(fs.readFileSync(bundlePath));
  console.error("jsdos: bundle " + path.basename(bundlePath) + " (" + bundle.length + " bytes), backend " + backend);

  if (typeof emulators[backend] !== "function") {
    console.error("jsdos: no such backend: " + backend);
    shutdown(2);
    return;
  }

  emulators[backend](bundle).then((instance) => {
    ci = instance;

    // onFrameSize does not fire on this backend, so take the dimensions from
    // the command interface and fall back to deriving them from the buffer.
    let width = 0;
    let height = 0;
    try {
      width = ci.width();
      height = ci.height();
    } catch (e) {
      width = height = 0;
    }
    console.error("jsdos: running, screen " + width + "x" + height);

    ci.events().onFrameSize((w, h) => {
      width = w;
      height = h;
      console.error("jsdos: frame size now " + w + "x" + h);
    });

    ci.events().onFrame((rgb, rgba) => {
      const buf = rgb || rgba;
      if (!buf) return;
      framesIn++;

      const channels = rgb ? 3 : 4;
      if (!width || !height || width * height * channels !== buf.length) {
        // Trust the buffer over the reported size; a mismatch here would
        // shear the picture rather than fail loudly.
        if (buf.length % (channels * 200) === 0 && buf.length / (channels * 200) === 320) {
          width = 320;
          height = 200;
        } else if (!width || !height) {
          return;
        }
      }
      sendFrame(buf, width, height, channels);
    });

    ci.events().onStdout((msg) => {
      if (process.env.MD_JSDOS_VERBOSE) process.stderr.write("dos: " + msg);
    });

    setInterval(() => {
      console.error("jsdos: " + framesIn + " frames in, " + framesSent +
                    " sent, " + framesDropped + " dropped (socket busy)");
      framesIn = framesSent = framesDropped = 0;
    }, 10000);
  }).catch((e) => {
    console.error("jsdos: failed to start: " + (e && e.stack ? e.stack : e));
    shutdown(2);
  });
}

let shuttingDown = false;

function shutdown(codeOut) {
  if (shuttingDown) return;
  shuttingDown = true;
  const done = () => process.exit(codeOut);
  try {
    sock.destroy();
  } catch (e) { /* ignore */ }
  if (ci) {
    Promise.resolve(ci.exit()).then(done, done);
    setTimeout(done, 1500);     // wasm teardown is best-effort
  } else {
    done();
  }
}

process.on("SIGTERM", () => shutdown(0));
process.on("SIGINT", () => shutdown(0));
