// Split the apartment glb into one glb per room + a manifest, for the webapp's
// progressive loader (scene.ts loadApartment). Pure split + prune -- NO texture
// re-encode, so total bytes and colors are identical to the monolith. Each room
// is one node/mesh/material/texture, so the split duplicates nothing.
//
//   node tools/split-apartment.mjs [srcGlb] [outDir]
//
// Defaults to the checkout's public/models (so viewer devs can regenerate with
// `npm run split:apartment`); publish_assets.py passes the staging paths.

import { NodeIO } from "@gltf-transform/core";
import { ALL_EXTENSIONS } from "@gltf-transform/extensions";
import { prune } from "@gltf-transform/functions";
import { mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(process.argv[2] ?? `${HERE}/../public/models/appartement.glb`);
const OUT = resolve(process.argv[3] ?? `${HERE}/../public/models/apartment`);

const io = new NodeIO().registerExtensions(ALL_EXTENSIONS);

/** World-space AABB of a scene node's mesh (glTF Y-up, matching the raw glb;
 * the viewer rotates the whole apartment Y-up -> Z-up, so the boxes ride along). */
function worldBBox(node) {
  const mesh = node.getMesh();
  if (!mesh) return null;
  const m = node.getWorldMatrix();
  const mn = [Infinity, Infinity, Infinity];
  const mx = [-Infinity, -Infinity, -Infinity];
  for (const prim of mesh.listPrimitives()) {
    const pos = prim.getAttribute("POSITION");
    if (!pos) continue;
    const a = pos.getArray();
    for (let i = 0; i < pos.getCount(); i++) {
      const x = a[i * 3];
      const y = a[i * 3 + 1];
      const z = a[i * 3 + 2];
      const w = [
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14],
      ];
      for (let k = 0; k < 3; k++) {
        mn[k] = Math.min(mn[k], w[k]);
        mx[k] = Math.max(mx[k], w[k]);
      }
    }
  }
  return { min: mn.map((v) => +v.toFixed(3)), max: mx.map((v) => +v.toFixed(3)) };
}

rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });

const roomCount = (await io.read(SRC)).getRoot().getDefaultScene().listChildren().length;
const rooms = [];
for (let i = 0; i < roomCount; i++) {
  const doc = await io.read(SRC);
  const scene = doc.getRoot().getDefaultScene();
  const kids = scene.listChildren();
  const keep = kids[i];
  const name = keep.getName() || `room${i}`;
  const bbox = worldBBox(keep);
  kids.forEach((k, j) => {
    if (j !== i) {
      scene.removeChild(k);
      k.dispose();
    }
  });
  await doc.transform(prune());
  const file = `${String(i).padStart(2, "0")}_${name}.glb`;
  await io.write(`${OUT}/${file}`, doc);
  rooms.push({ file, name, bytes: statSync(`${OUT}/${file}`).size, bbox });
}

const total = rooms.reduce((sum, r) => sum + r.bytes, 0);
writeFileSync(`${OUT}/manifest.json`, JSON.stringify({ rooms, total }, null, 2) + "\n");
console.log(`split ${SRC} -> ${rooms.length} rooms, ${(total / 1e6).toFixed(1)} MB, no re-encode`);
