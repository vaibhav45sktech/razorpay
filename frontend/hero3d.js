/* CampusHood hero — an original 3D composition in the spirit of a floating
   cluster of labelled, glossy blocks. Built with three.js (vendored, MIT).
   Seven rounded blocks, each a different finish (glass, satin, speckled,
   chrome), labelled with the product's building blocks. Slow group rotation,
   gentle per-block bobbing, subtle pointer parallax. Respects
   prefers-reduced-motion (static pose). No network, no external assets. */
(() => {
  "use strict";
  const canvas = document.getElementById("heroCanvas");
  if (!canvas || typeof THREE === "undefined") return;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputEncoding = THREE.sRGBEncoding;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);
  camera.position.set(0, 1.4, 11.5);
  camera.lookAt(0, 0, 0);

  // ---- environment: a soft studio built from gradients, so metals & glass have something to reflect
  function gradientFace(top, bottom, streak) {
    const c = document.createElement("canvas"); c.width = c.height = 256; const g = c.getContext("2d");
    const grd = g.createLinearGradient(0, 0, 0, 256); grd.addColorStop(0, top); grd.addColorStop(1, bottom);
    g.fillStyle = grd; g.fillRect(0, 0, 256, 256);
    if (streak) { g.fillStyle = streak; g.globalAlpha = .55; g.fillRect(40, 60, 176, 26); g.globalAlpha = 1; }
    return c;
  }
  const faces = [
    gradientFace("#3a3f44", "#0b0b0b", "#ffffff"), gradientFace("#2a2e33", "#0b0b0b"),
    gradientFace("#f2f4f2", "#8a9096"), gradientFace("#0b0b0b", "#050505"),
    gradientFace("#33383d", "#0b0b0b", "#c8ff00"), gradientFace("#25292d", "#0b0b0b"),
  ];
  const envTex = new THREE.CubeTexture(faces); envTex.needsUpdate = true; envTex.encoding = THREE.sRGBEncoding;
  const pmrem = new THREE.PMREMGenerator(renderer); pmrem.compileCubemapShader();
  scene.environment = pmrem.fromCubemap(envTex).texture;

  // ---- lights
  scene.add(new THREE.HemisphereLight(0xffffff, 0x0a0a0a, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 1.35); key.position.set(4, 7, 6); key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024); key.shadow.radius = 6; scene.add(key);
  const rim = new THREE.DirectionalLight(0xc8ff00, 0.9); rim.position.set(-6, 3, -4); scene.add(rim);
  const fill = new THREE.PointLight(0x8fd0ff, 0.5, 30); fill.position.set(-3, -2, 5); scene.add(fill);

  // ---- rounded box geometry (r128 has RoundedBoxGeometry only in examples; build a simple one)
  function roundedBox(w, h, d, r, seg = 4) {
    // Extrude a rounded rectangle and bevel — good enough silhouette, cheap.
    const shape = new THREE.Shape();
    const x = -w / 2, y = -h / 2;
    shape.moveTo(x + r, y); shape.lineTo(x + w - r, y); shape.quadraticCurveTo(x + w, y, x + w, y + r);
    shape.lineTo(x + w, y + h - r); shape.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    shape.lineTo(x + r, y + h); shape.quadraticCurveTo(x, y + h, x, y + h - r);
    shape.lineTo(x, y + r); shape.quadraticCurveTo(x, y, x + r, y);
    const geo = new THREE.ExtrudeGeometry(shape, { depth: d - 2 * r, bevelEnabled: true, bevelThickness: r, bevelSize: r, bevelSegments: seg, curveSegments: 10 });
    geo.center(); return geo;
  }

  // ---- label textures (drawn on canvas; text is ours)
  function labelTexture(text, { fg = "#111", bg = "#f4f4f2", speckle = null, font = "600 92px Inter, system-ui, sans-serif", align = "left" } = {}) {
    const c = document.createElement("canvas"); c.width = 1024; c.height = 1024; const g = c.getContext("2d");
    g.fillStyle = bg; g.fillRect(0, 0, 1024, 1024);
    if (speckle) { g.fillStyle = speckle; for (let i = 0; i < 1400; i++) { g.globalAlpha = Math.random() * .5; g.fillRect(Math.random() * 1024, Math.random() * 1024, 2 + Math.random() * 3, 2 + Math.random() * 3); } g.globalAlpha = 1; }
    g.fillStyle = fg; g.font = font; g.textBaseline = "top"; g.textAlign = align;
    g.fillText(text, align === "left" ? 80 : 512, 96);
    const t = new THREE.CanvasTexture(c); t.encoding = THREE.sRGBEncoding; t.anisotropy = 8; return t;
  }

  // ---- the blocks: label, size, position, finish
  const B = [
    { t: "Savings", s: [2.2, 2.2, 2.2], p: [-2.9, 0.75, -0.4], fin: "satin", bg: "#efece3", fg: "#141414", font: "italic 400 110px 'Iowan Old Style', Georgia, serif" },
    { t: "Pool", s: [1.9, 1.1, 1.9], p: [-0.35, 1.35, 0.3], fin: "glass", bg: "#1f8fff", fg: "#ffffff", speckle: "#0b3d91" },
    { t: "Rules", s: [1.9, 1.0, 1.9], p: [-0.35, 0.15, 0.3], fin: "glass", bg: "#1f8fff", fg: "#ffffff", speckle: "#0b3d91" },
    { t: "Agent", s: [2.1, 2.2, 2.1], p: [1.75, 0.75, -0.5], fin: "satin", bg: "#ffffff", fg: "#141414" },
    { t: "Rewards", s: [2.4, 2.4, 2.4], p: [3.85, 0.4, -1.6], fin: "speckled", bg: "#bfe1ff", fg: "#0b3d91", speckle: "#2a6fd6" },
    { t: "Offers", s: [1.0, 1.0, 1.0], p: [-3.5, -1.65, 0.6], fin: "gloss", bg: "#ff3e7f", fg: "#1a0a10", cluster: true },
    { t: "Audit", s: [2.2, 2.2, 2.2], p: [-0.85, -1.75, 0.2], fin: "satin", bg: "#f3f2ea", fg: "#141414" },
    { t: "Razorpay", s: [2.1, 1.9, 2.1], p: [1.45, -1.9, -0.2], fin: "chrome", bg: "#9aa0a6", fg: "#ffffff" },
  ];
  function material(b) {
    const map = labelTexture(b.t, { fg: b.fg, bg: b.bg, speckle: b.speckle || null, font: b.font || "600 96px Inter, system-ui, sans-serif" });
    const common = { map, envMapIntensity: 1.1 };
    switch (b.fin) {
      case "glass": return new THREE.MeshPhysicalMaterial({ ...common, roughness: .12, metalness: 0, transmission: .35, thickness: 1.2, clearcoat: 1, clearcoatRoughness: .08, ior: 1.4 });
      case "gloss": return new THREE.MeshPhysicalMaterial({ ...common, roughness: .08, metalness: .05, clearcoat: 1, clearcoatRoughness: .05 });
      case "chrome": return new THREE.MeshStandardMaterial({ ...common, roughness: .18, metalness: .85 });
      case "speckled": return new THREE.MeshPhysicalMaterial({ ...common, roughness: .35, metalness: .05, clearcoat: .6, clearcoatRoughness: .3 });
      default: return new THREE.MeshPhysicalMaterial({ ...common, roughness: .42, metalness: 0, clearcoat: .35, clearcoatRoughness: .4, sheen: .4, sheenColor: new THREE.Color("#ffffff") });
    }
  }
  const group = new THREE.Group(); scene.add(group);
  const items = [];
  B.forEach((b, i) => {
    const mat = material(b);
    if (b.cluster) { // four small glossy blocks (the "Offers" tile)
      const r = b.s[0] * .5 + .05;
      [[-r, r], [r, r], [-r, -r], [r, -r]].forEach(([dx, dy], k) => {
        const m = new THREE.Mesh(roundedBox(...b.s, .16), mat); m.castShadow = m.receiveShadow = true;
        m.position.set(b.p[0] + dx, b.p[1] + dy, b.p[2]); group.add(m); items.push({ m, phase: i + k * .7, amp: .06 });
      });
      return;
    }
    const m = new THREE.Mesh(roundedBox(...b.s, .18), mat); m.castShadow = m.receiveShadow = true;
    m.position.set(...b.p); group.add(m); items.push({ m, phase: i * 1.3, amp: .08 });
  });
  group.rotation.set(0.32, -0.55, 0.04);
  group.position.set(0.2, 0.1, 0);

  // soft ground shadow catcher
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(30, 30), new THREE.ShadowMaterial({ opacity: .35 }));
  ground.rotation.x = -Math.PI / 2; ground.position.y = -3.4; ground.receiveShadow = true; scene.add(ground);

  // ---- resize + pointer parallax + loop
  let w = 0, h = 0;
  function resize() {
    const r = canvas.parentElement.getBoundingClientRect();
    if (r.width === w && r.height === h) return;
    w = r.width; h = r.height; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  let px = 0, py = 0, tx = 0, ty = 0;
  addEventListener("pointermove", (e) => { tx = (e.clientX / innerWidth - .5) * .25; ty = (e.clientY / innerHeight - .5) * .15; }, { passive: true });

  let visible = true;
  new IntersectionObserver((es) => es.forEach((e) => { visible = e.isIntersecting; }), { threshold: 0 }).observe(canvas);

  const start = performance.now();
  function frame(now) {
    requestAnimationFrame(frame);
    if (!visible) return;
    resize();
    const t = (now - start) / 1000;
    if (!reduce) {
      px += (tx - px) * .04; py += (ty - py) * .04;
      group.rotation.y = -0.55 + Math.sin(t * .18) * .22 + px;
      group.rotation.x = 0.32 + Math.sin(t * .13) * .06 + py;
      items.forEach(({ m, phase, amp }) => { m.position.y += Math.sin(t * 1.1 + phase) * amp * .02; m.rotation.z = Math.sin(t * .6 + phase) * .01; });
    }
    renderer.render(scene, camera);
  }
  resize(); requestAnimationFrame(frame);
})();
