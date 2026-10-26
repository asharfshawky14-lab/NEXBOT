
import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js";

const stage = document.querySelector(".robot-stage");

if (stage && !document.querySelector("#robot3d-canvas")) {
  const canvas = document.createElement("canvas");
  canvas.id = "robot3d-canvas";
  canvas.setAttribute("aria-label", "روبوت NEXBOT ثلاثي الأبعاد");
  canvas.style.cssText =
    "position:absolute;inset:0;width:100%;height:100%;display:block;z-index:3;";

  stage.appendChild(canvas);

  // Scene
  const scene = new THREE.Scene();
  scene.background = null;

  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
  camera.position.set(0, 2.1, 10.5);
  camera.lookAt(0, 2.1, 0);

  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: "high-performance"
  });

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;

  // Lighting
  scene.add(new THREE.HemisphereLight(0xc9f8ff, 0x101629, 2.1));

  const keyLight = new THREE.DirectionalLight(0xffffff, 3.2);
  keyLight.position.set(4, 7, 6);
  scene.add(keyLight);

  const cyanLight = new THREE.PointLight(0x00eaff, 20, 12);
  cyanLight.position.set(-3, 3.3, 3);
  scene.add(cyanLight);

  const blueLight = new THREE.PointLight(0x4c5cff, 15, 12);
  blueLight.position.set(3, 2, -2);
  scene.add(blueLight);

  // Materials
  const metal = new THREE.MeshStandardMaterial({
    color: 0xaebdca,
    metalness: 0.88,
    roughness: 0.24
  });

  const lightMetal = new THREE.MeshStandardMaterial({
    color: 0xe5f4fa,
    metalness: 0.78,
    roughness: 0.2
  });

  const darkMetal = new THREE.MeshStandardMaterial({
    color: 0x182532,
    metalness: 0.85,
    roughness: 0.3
  });

  const cyan = new THREE.MeshStandardMaterial({
    color: 0x25f4ff,
    emissive: 0x00cbdc,
    emissiveIntensity: 2.5,
    metalness: 0.45,
    roughness: 0.2
  });

  const visorMaterial = new THREE.MeshStandardMaterial({
    color: 0x061522,
    emissive: 0x003f58,
    emissiveIntensity: 1.1,
    metalness: 0.65,
    roughness: 0.15
  });

  const robot = new THREE.Group();
  scene.add(robot);

  function addMesh(parent, geometry, material, position, rotation) {
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(...position);
    if (rotation) mesh.rotation.set(...rotation);
    mesh.castShadow = false;
    mesh.receiveShadow = false;
    parent.add(mesh);
    return mesh;
  }

  function box(parent, size, material, position, bevel = 0) {
    const geometry = new THREE.BoxGeometry(...size);
    if (bevel > 0) {
      const edges = new THREE.EdgesGeometry(geometry);
      const outline = new THREE.LineSegments(
        edges,
        new THREE.LineBasicMaterial({
          color: 0x5bfcff,
          transparent: true,
          opacity: 0.38
        })
      );
      outline.position.set(...position);
      parent.add(outline);
    }
    return addMesh(parent, geometry, material, position);
  }

  function sphere(parent, radius, material, position, scale = [1, 1, 1]) {
    const mesh = addMesh(
      parent,
      new THREE.SphereGeometry(radius, 20, 14),
      material,
      position
    );
    mesh.scale.set(...scale);
    return mesh;
  }

  function cylinder(parent, top, bottom, height, material, position, segments = 16) {
    return addMesh(
      parent,
      new THREE.CylinderGeometry(top, bottom, height, segments),
      material,
      position
    );
  }

  function joint(parent, position, radius = 0.15) {
    sphere(parent, radius, darkMetal, position);
    sphere(parent, radius * 0.52, cyan, [
      position[0],
      position[1],
      position[2] + radius * 0.72
    ]);
  }

  // Waist and torso
  const waist = new THREE.Group();
  waist.position.y = 2.05;
  robot.add(waist);

  box(waist, [0.9, 0.48, 0.48], darkMetal, [0, 0, 0.02], 1);
  box(waist, [0.62, 0.2, 0.53], metal, [0, 0.05, 0.02]);
  sphere(waist, 0.12, cyan, [0, 0.06, 0.3], [1, 0.55, 0.45]);

  const torso = new THREE.Group();
  torso.position.y = 0.48;
  waist.add(torso);

  box(torso, [1.34, 1.18, 0.7], metal, [0, 0.38, 0], 1);
  box(torso, [1.08, 0.83, 0.16], darkMetal, [0, 0.39, 0.38]);
  box(torso, [0.72, 0.53, 0.06], visorMaterial, [0, 0.42, 0.49]);

  // Glowing chest core
  const coreRing = addMesh(
    torso,
    new THREE.TorusGeometry(0.19, 0.035, 8, 28),
    cyan,
    [0, 0.46, 0.55]
  );

  sphere(torso, 0.115, cyan, [0, 0.46, 0.54], [1, 1, 0.35]);

  for (let i = 0; i < 3; i++) {
    box(
      torso,
      [0.09, 0.035, 0.025],
      cyan,
      [-0.31 + i * 0.31, 0.06, 0.49]
    );
  }

  box(torso, [0.32, 0.08, 0.035], lightMetal, [0, 0.87, 0.39]);

  // Neck and head pivot
  cylinder(robot, 0.17, 0.2, 0.25, darkMetal, [0, 3.22, 0]);
  const headPivot = new THREE.Group();
  headPivot.position.set(0, 3.36, 0);
  robot.add(headPivot);

  box(headPivot, [0.94, 0.76, 0.7], lightMetal, [0, 0.14, 0]);
  box(headPivot, [0.77, 0.39, 0.09], visorMaterial, [0, 0.11, 0.37]);

  // Two luminous eyes
  for (const x of [-0.2, 0.2]) {
    box(headPivot, [0.13, 0.055, 0.035], cyan, [x, 0.14, 0.43]);
  }

  box(headPivot, [0.22, 0.035, 0.025], darkMetal, [0, -0.1, 0.43]);

  // Side ears and antenna
  for (const side of [-1, 1]) {
    box(headPivot, [0.14, 0.27, 0.3], metal, [side * 0.52, 0.12, 0]);
    box(headPivot, [0.045, 0.12, 0.08], cyan, [side * 0.6, 0.12, 0.18]);
  }

  cylinder(headPivot, 0.035, 0.055, 0.24, darkMetal, [0, 0.62, 0]);
  sphere(headPivot, 0.075, cyan, [0, 0.76, 0]);

  // Shoulder pivots and articulated arms
  const arms = [];

  for (const side of [-1, 1]) {
    const shoulder = new THREE.Group();
    shoulder.position.set(side * 0.79, 2.82, 0);
    robot.add(shoulder);

    sphere(shoulder, 0.25, lightMetal, [0, 0, 0], [1, 1, 0.9]);
    sphere(shoulder, 0.14, cyan, [side * 0.05, 0, 0.19]);

    cylinder(
      shoulder,
      0.15,
      0.19,
      0.56,
      metal,
      [side * 0.06, -0.35, 0],
      14
    );

    joint(shoulder, [side * 0.06, -0.66, 0.02], 0.17);

    const elbow = new THREE.Group();
    elbow.position.set(side * 0.06, -0.69, 0.02);
    shoulder.add(elbow);

    cylinder(
      elbow,
      0.13,
      0.17,
      0.48,
      lightMetal,
      [side * 0.04, -0.26, 0]
    );

    joint(elbow, [side * 0.04, -0.52, 0.03], 0.13);

    const hand = new THREE.Group();
    hand.position.set(side * 0.04, -0.61, 0.03);
    elbow.add(hand);

    box(hand, [0.24, 0.25, 0.19], darkMetal, [0, -0.03, 0.02]);

    for (let finger = -1; finger <= 1; finger++) {
      box(
        hand,
        [0.055, 0.15, 0.07],
        lightMetal,
        [finger * 0.07, -0.22, 0.05]
      );
    }

    box(hand, [0.07, 0.13, 0.07], metal, [side * 0.17, -0.06, 0.06]);

    arms.push({ side, shoulder, elbow, hand });
  }

  // Legs with knees, ankle joints and feet
  const legs = [];

  for (const side of [-1, 1]) {
    const hip = new THREE.Group();
    hip.position.set(side * 0.34, 1.82, 0);
    robot.add(hip);

    joint(hip, [0, 0, 0], 0.18);

    cylinder(hip, 0.2, 0.16, 0.57, metal, [0, -0.36, 0], 14);
    box(hip, [0.27, 0.32, 0.3], lightMetal, [0, -0.36, 0.08]);

    const knee = new THREE.Group();
    knee.position.set(0, -0.68, 0.02);
    hip.add(knee);

    joint(knee, [0, 0, 0], 0.16);
    cylinder(knee, 0.13, 0.16, 0.52, metal, [0, -0.3, 0]);
    box(knee, [0.24, 0.32, 0.27], darkMetal, [0, -0.3, 0.07]);

    const foot = new THREE.Group();
    foot.position.set(0, -0.61, 0.08);
    knee.add(foot);

    sphere(foot, 0.13, cyan, [0, 0.04, 0.02], [1, 0.65, 0.75]);
    box(foot, [0.4, 0.17, 0.62], lightMetal, [0, -0.08, 0.16]);
    box(foot, [0.34, 0.05, 0.38], darkMetal, [0, -0.17, 0.2]);

    legs.push({ side, hip, knee, foot });
  }

  // Floating holographic details
  const particles = new THREE.Group();
  scene.add(particles);

  for (let i = 0; i < 34; i++) {
    const angle = i * 2.399;
    const radius = 1.65 + (i % 5) * 0.18;
    const bead = sphere(
      particles,
      0.018 + (i % 3) * 0.008,
      i % 2 ? cyan : lightMetal,
      [
        Math.cos(angle) * radius,
        0.6 + (i % 12) * 0.34,
        Math.sin(angle) * 0.45
      ]
    );
    bead.userData.phase = i * 0.7;
  }

  // Ground halo
  const halo = addMesh(
    scene,
    new THREE.TorusGeometry(1.35, 0.018, 8, 80),
    cyan,
    [0, 0.1, 0],
    [-Math.PI / 2, 0, 0]
  );
  halo.scale.set(1.1, 0.65, 1);

  const floorGlow = new THREE.PointLight(0x00dfff, 7, 5);
  floorGlow.position.set(0, 0.5, 1);
  scene.add(floorGlow);

  // Responsive canvas
  function resize() {
    const width = Math.max(stage.clientWidth, 1);
    const height = Math.max(stage.clientHeight, 1);

    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.position.z = width < 430 ? 11.8 : 10.5;
    camera.updateProjectionMatrix();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(stage);
  resize();

  // Animation
  const clock = new THREE.Clock();

  function animate() {
    requestAnimationFrame(animate);

    const t = clock.getElapsedTime();

    robot.position.y = Math.sin(t * 1.15) * 0.055;
    robot.rotation.y = Math.sin(t * 0.42) * 0.12;
    torso.rotation.z = Math.sin(t * 0.8) * 0.018;
    headPivot.rotation.y = Math.sin(t * 0.65) * 0.15;
    headPivot.rotation.x = Math.sin(t * 0.9) * 0.035;

    arms.forEach(({ side, shoulder, elbow }) => {
      shoulder.rotation.z =
        side * (0.06 + Math.sin(t * 1.05 + side) * 0.045);
      elbow.rotation.z = side * (0.12 + Math.sin(t * 0.8 + side) * 0.08);
    });

    legs.forEach(({ side, hip, knee }) => {
      hip.rotation.z = Math.sin(t * 0.9 + side) * 0.025;
      knee.rotation.z = Math.max(0, Math.sin(t * 0.9 + side) * 0.035);
    });

    coreRing.rotation.z = t * 0.55;
    cyanLight.intensity = 17 + Math.sin(t * 2) * 3;
    halo.rotation.z = -t * 0.18;

    particles.children.forEach((particle) => {
      particle.position.y += Math.sin(t + particle.userData.phase) * 0.0008;
    });

    renderer.render(scene, camera);
  }

  animate();

  // Respect reduced-motion preference
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    robot.position.y = 0;
    robot.rotation.y = 0;
  }

  // Hide the old CSS robot only when the 3D canvas is active.
  const oldRobot = stage.querySelector(".robot-placeholder");
  if (oldRobot) oldRobot.style.display = "none";
}
