import {
  BackSide,
  CanvasTexture,
  Color,
  DoubleSide,
  Mesh,
  MeshBasicMaterial,
  PlaneGeometry,
  PMREMGenerator,
  RepeatWrapping,
  Scene,
  ShaderMaterial,
  SphereGeometry,
  SRGBColorSpace,
  type Texture,
  type WebGLRenderer,
} from 'three';
import { mulberry32 } from './utils';

function makeCanvas(width: number, height = width) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

function asTexture(canvas: HTMLCanvasElement, srgb = true) {
  const texture = new CanvasTexture(canvas);
  texture.wrapS = RepeatWrapping;
  texture.wrapT = RepeatWrapping;
  texture.anisotropy = 8;
  if (srgb) {
    texture.colorSpace = SRGBColorSpace;
  }
  return texture;
}

interface Lattice {
  size: number;
  values: Float32Array;
}

function makeLattice(size: number, random: () => number): Lattice {
  const values = new Float32Array(size * size);
  for (let i = 0; i < values.length; i += 1) {
    values[i] = random();
  }
  return { size, values };
}

function sampleLattice(lattice: Lattice, u: number, v: number) {
  const { size, values } = lattice;
  const x = u * size;
  const y = v * size;
  const x0 = Math.floor(x) % size;
  const y0 = Math.floor(y) % size;
  const x1 = (x0 + 1) % size;
  const y1 = (y0 + 1) % size;
  const tx = x - Math.floor(x);
  const ty = y - Math.floor(y);
  const sx = tx * tx * (3 - 2 * tx);
  const sy = ty * ty * (3 - 2 * ty);
  const a = values[y0 * size + x0] ?? 0;
  const b = values[y0 * size + x1] ?? 0;
  const c = values[y1 * size + x0] ?? 0;
  const d = values[y1 * size + x1] ?? 0;
  const top = a + (b - a) * sx;
  const bottom = c + (d - c) * sx;
  return top + (bottom - top) * sy;
}

function fbm(lattices: readonly Lattice[], u: number, v: number) {
  let value = 0;
  let amplitude = 0.55;
  let total = 0;
  for (const lattice of lattices) {
    value += sampleLattice(lattice, u, v) * amplitude;
    total += amplitude;
    amplitude *= 0.5;
  }
  return value / total;
}

export interface SurfaceTextures {
  map: Texture;
  bump: Texture;
  roughness: Texture;
}

export function createConcreteTextures(seed = 7): SurfaceTextures {
  const size = 512;
  const random = mulberry32(seed);
  const lattices = [8, 16, 32, 64, 128].map((grid) => makeLattice(grid, random));
  const albedo = makeCanvas(size);
  const bump = makeCanvas(size);
  const rough = makeCanvas(size);
  const albedoContext = albedo.getContext('2d');
  const bumpContext = bump.getContext('2d');
  const roughContext = rough.getContext('2d');
  if (!albedoContext || !bumpContext || !roughContext) {
    throw new Error('无法创建程序化材质画布');
  }

  const albedoImage = albedoContext.createImageData(size, size);
  const bumpImage = bumpContext.createImageData(size, size);
  const roughImage = roughContext.createImageData(size, size);

  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const u = x / size;
      const v = y / size;
      const grain = fbm(lattices, u, v);
      const fine = random() * 0.16;
      const value = 19 + grain * 27 + fine * 12;
      const index = (y * size + x) * 4;
      albedoImage.data[index] = value * 1.04;
      albedoImage.data[index + 1] = value * 1.0;
      albedoImage.data[index + 2] = value * 1.02;
      albedoImage.data[index + 3] = 255;

      const height = 96 + grain * 120 + fine * 45;
      bumpImage.data[index] = height;
      bumpImage.data[index + 1] = height;
      bumpImage.data[index + 2] = height;
      bumpImage.data[index + 3] = 255;

      const roughness = 128 + grain * 72 - fine * 30;
      roughImage.data[index] = roughness;
      roughImage.data[index + 1] = roughness;
      roughImage.data[index + 2] = roughness;
      roughImage.data[index + 3] = 255;
    }
  }

  albedoContext.putImageData(albedoImage, 0, 0);
  bumpContext.putImageData(bumpImage, 0, 0);
  roughContext.putImageData(roughImage, 0, 0);

  albedoContext.lineCap = 'round';
  for (let i = 0; i < 24; i += 1) {
    let x = random() * size;
    let y = random() * size;
    albedoContext.beginPath();
    albedoContext.moveTo(x, y);
    const length = 20 + random() * 90;
    for (let step = 0; step < 5; step += 1) {
      x += (random() - 0.5) * length * 0.4;
      y += length * 0.25;
      albedoContext.lineTo(x, y);
    }
    albedoContext.strokeStyle = `rgba(3, 5, 8, ${0.12 + random() * 0.2})`;
    albedoContext.lineWidth = 0.6 + random() * 1.4;
    albedoContext.stroke();
  }

  for (let i = 0; i < 180; i += 1) {
    const x = random() * size;
    const y = random() * size;
    const radius = random() * 1.8;
    albedoContext.fillStyle = `rgba(180, 210, 255, ${random() * 0.045})`;
    albedoContext.beginPath();
    albedoContext.arc(x, y, radius, 0, Math.PI * 2);
    albedoContext.fill();
  }

  const map = asTexture(albedo);
  map.repeat.set(6, 6);
  const bumpTexture = asTexture(bump, false);
  bumpTexture.repeat.set(6, 6);
  const roughnessTexture = asTexture(rough, false);
  roughnessTexture.repeat.set(6, 6);

  return { map, bump: bumpTexture, roughness: roughnessTexture };
}

export function createMetalTexture(seed = 31): SurfaceTextures {
  const size = 512;
  const random = mulberry32(seed);
  const albedo = makeCanvas(size);
  const bump = makeCanvas(size);
  const rough = makeCanvas(size);
  const context = albedo.getContext('2d');
  const bumpContext = bump.getContext('2d');
  const roughContext = rough.getContext('2d');
  if (!context || !bumpContext || !roughContext) {
    throw new Error('无法创建程序化金属材质');
  }

  context.fillStyle = '#171c23';
  context.fillRect(0, 0, size, size);
  bumpContext.fillStyle = '#777777';
  bumpContext.fillRect(0, 0, size, size);
  roughContext.fillStyle = '#6f6f6f';
  roughContext.fillRect(0, 0, size, size);

  for (let i = 0; i < 900; i += 1) {
    const y = random() * size;
    const shade = 22 + random() * 38;
    context.strokeStyle = `rgba(${shade}, ${shade + 5}, ${shade + 12}, ${0.06 + random() * 0.13})`;
    context.lineWidth = 0.5 + random();
    context.beginPath();
    context.moveTo(0, y);
    context.lineTo(size, y + (random() - 0.5) * 2);
    context.stroke();
  }

  context.strokeStyle = 'rgba(0, 0, 0, 0.55)';
  context.lineWidth = 2;
  for (let i = 1; i < 4; i += 1) {
    context.beginPath();
    context.moveTo((size / 4) * i, 0);
    context.lineTo((size / 4) * i, size);
    context.stroke();
  }

  for (let i = 0; i < 60; i += 1) {
    const x = (Math.floor(random() * 8) + 0.5) * (size / 8);
    const y = (Math.floor(random() * 8) + 0.5) * (size / 8);
    context.fillStyle = 'rgba(190, 205, 220, 0.1)';
    context.beginPath();
    context.arc(x, y, 1.5, 0, Math.PI * 2);
    context.fill();
    bumpContext.fillStyle = 'rgba(255, 255, 255, 0.6)';
    bumpContext.beginPath();
    bumpContext.arc(x, y, 2, 0, Math.PI * 2);
    bumpContext.fill();
  }

  const map = asTexture(albedo);
  map.repeat.set(2, 2);
  const bumpTexture = asTexture(bump, false);
  bumpTexture.repeat.set(2, 2);
  const roughnessTexture = asTexture(rough, false);
  roughnessTexture.repeat.set(2, 2);

  return { map, bump: bumpTexture, roughness: roughnessTexture };
}

export function createPuddleAlpha(seed = 91) {
  const size = 1024;
  const random = mulberry32(seed);
  const canvas = makeCanvas(size);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建水洼遮罩');
  }

  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, size, size);

  for (let i = 0; i < 46; i += 1) {
    const x = random() * size;
    const y = random() * size;
    const radiusX = 28 + random() * 150;
    const radiusY = radiusX * (0.25 + random() * 0.7);
    context.save();
    context.translate(x, y);
    context.rotate(random() * Math.PI);
    context.scale(1, radiusY / radiusX);
    const gradient = context.createRadialGradient(0, 0, 0, 0, 0, radiusX);
    gradient.addColorStop(0, 'rgba(0, 0, 0, 0.96)');
    gradient.addColorStop(0.55, 'rgba(0, 0, 0, 0.72)');
    gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');
    context.fillStyle = gradient;
    context.beginPath();
    context.arc(0, 0, radiusX, 0, Math.PI * 2);
    context.fill();
    context.restore();
  }

  const texture = asTexture(canvas, false);
  texture.repeat.set(1.4, 1.4);
  return texture;
}

export function createGlowTexture(inner = 'rgba(255,255,255,1)') {
  const size = 256;
  const canvas = makeCanvas(size);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建光晕贴图');
  }
  const gradient = context.createRadialGradient(
    size / 2,
    size / 2,
    0,
    size / 2,
    size / 2,
    size / 2,
  );
  gradient.addColorStop(0, inner);
  gradient.addColorStop(0.16, 'rgba(190, 245, 255, 0.82)');
  gradient.addColorStop(0.44, 'rgba(80, 190, 255, 0.22)');
  gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');
  context.fillStyle = gradient;
  context.fillRect(0, 0, size, size);
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

export function createSmokeTexture(seed = 61) {
  const size = 256;
  const random = mulberry32(seed);
  const canvas = makeCanvas(size);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建烟雾贴图');
  }
  for (let i = 0; i < 80; i += 1) {
    const x = size / 2 + (random() - 0.5) * size * 0.52;
    const y = size / 2 + (random() - 0.5) * size * 0.52;
    const radius = 12 + random() * 65;
    const gradient = context.createRadialGradient(x, y, 0, x, y, radius);
    const alpha = 0.018 + random() * 0.035;
    gradient.addColorStop(0, `rgba(220, 235, 245, ${alpha})`);
    gradient.addColorStop(1, 'rgba(220, 235, 245, 0)');
    context.fillStyle = gradient;
    context.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

export function createContactShadowTexture() {
  const size = 256;
  const canvas = makeCanvas(size);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建接触阴影贴图');
  }
  const gradient = context.createRadialGradient(
    size / 2,
    size / 2,
    2,
    size / 2,
    size / 2,
    size / 2,
  );
  gradient.addColorStop(0, 'rgba(0, 0, 0, 0.92)');
  gradient.addColorStop(0.45, 'rgba(0, 0, 0, 0.62)');
  gradient.addColorStop(0.78, 'rgba(0, 0, 0, 0.18)');
  gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');
  context.fillStyle = gradient;
  context.fillRect(0, 0, size, size);
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

export function createScorchTexture() {
  const size = 256;
  const canvas = makeCanvas(size);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建弹痕贴图');
  }
  const gradient = context.createRadialGradient(
    size / 2,
    size / 2,
    2,
    size / 2,
    size / 2,
    size / 2,
  );
  gradient.addColorStop(0, 'rgba(0, 0, 0, 0.95)');
  gradient.addColorStop(0.25, 'rgba(2, 4, 7, 0.62)');
  gradient.addColorStop(0.72, 'rgba(4, 6, 9, 0.16)');
  gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');
  context.fillStyle = gradient;
  context.fillRect(0, 0, size, size);
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

export function createHazardTexture() {
  const size = 512;
  const canvas = makeCanvas(size);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建警示贴图');
  }
  context.fillStyle = '#11151a';
  context.fillRect(0, 0, size, size);
  context.save();
  context.translate(size / 2, size / 2);
  context.rotate(-Math.PI / 4);
  for (let i = -size; i < size; i += 72) {
    context.fillStyle = i % 144 === 0 ? '#d9a526' : '#1a1d21';
    context.fillRect(i, -size, 36, size * 2);
  }
  context.restore();
  context.fillStyle = 'rgba(0, 0, 0, 0.35)';
  for (let i = 0; i < 40; i += 1) {
    context.fillRect(Math.random() * size, Math.random() * size, 3, 3);
  }
  const texture = asTexture(canvas);
  texture.repeat.set(2, 1);
  return texture;
}

export function createBillboardTexture(title: string, subtitle: string, accent: string) {
  const width = 1024;
  const height = 512;
  const canvas = makeCanvas(width, height);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建广告牌贴图');
  }

  context.clearRect(0, 0, width, height);
  context.fillStyle = 'rgba(5, 9, 15, 0.92)';
  context.fillRect(18, 18, width - 36, height - 36);
  context.strokeStyle = accent;
  context.lineWidth = 6;
  context.strokeRect(18, 18, width - 36, height - 36);
  context.fillStyle = accent;
  context.fillRect(50, 56, 64, 8);
  context.fillRect(50, height - 70, 240, 4);

  context.textAlign = 'left';
  context.fillStyle = 'rgba(228, 246, 255, 0.96)';
  context.font = '800 164px "Arial Narrow", "Helvetica Neue", sans-serif';
  context.fillText(title, 48, 236);
  context.fillStyle = accent;
  context.font = '600 46px "Arial Narrow", "PingFang SC", sans-serif';
  context.fillText(subtitle, 52, 306);
  context.fillStyle = 'rgba(190, 224, 240, 0.7)';
  context.font = '500 26px "Helvetica Neue", sans-serif';
  context.fillText('KIRIN HEAVY INDUSTRIES // SECTOR 07', 52, 374);

  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.anisotropy = 8;
  return texture;
}

export function createBuildingTexture(seed = 17) {
  const width = 256;
  const height = 512;
  const random = mulberry32(seed);
  const canvas = makeCanvas(width, height);
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('无法创建楼宇贴图');
  }
  context.fillStyle = '#04060a';
  context.fillRect(0, 0, width, height);
  const cols = 8;
  const rows = 24;
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const lit = random() > 0.58;
      if (!lit) {
        continue;
      }
      const warm = random() > 0.55;
      context.fillStyle = warm ? 'rgba(255, 185, 110, 0.85)' : 'rgba(130, 220, 255, 0.8)';
      context.fillRect(18 + col * 29, 22 + row * 20, 11, 7);
    }
  }
  for (let i = 0; i < 3; i += 1) {
    context.fillStyle = i === 1 ? 'rgba(255, 70, 160, 0.45)' : 'rgba(70, 210, 255, 0.35)';
    context.fillRect(0, 90 + i * 150, width, 3);
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.wrapS = RepeatWrapping;
  texture.wrapT = RepeatWrapping;
  return texture;
}

export function createEnvironmentTexture(renderer: WebGLRenderer) {
  const envScene = new Scene();
  envScene.background = new Color(0x02040a);

  const sky = new Mesh(
    new SphereGeometry(60, 32, 16),
    new ShaderMaterial({
      side: BackSide,
      vertexShader: `
        varying vec3 vPosition;
        void main() {
          vPosition = position;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        varying vec3 vPosition;
        void main() {
          float h = normalize(vPosition).y * 0.5 + 0.5;
          vec3 low = vec3(0.005, 0.012, 0.025);
          vec3 high = vec3(0.025, 0.06, 0.11);
          gl_FragColor = vec4(mix(low, high, h), 1.0);
        }
      `,
    }),
  );
  envScene.add(sky);

  const panel = (color: Color, position: [number, number, number], scale: [number, number]) => {
    const mesh = new Mesh(
      new PlaneGeometry(scale[0], scale[1]),
      new MeshBasicMaterial({ color, side: DoubleSide }),
    );
    mesh.position.set(...position);
    mesh.lookAt(0, 2, 0);
    envScene.add(mesh);
  };

  panel(new Color().setRGB(0.45, 3.2, 4.6), [18, 5, -6], [26, 8]);
  panel(new Color().setRGB(3.4, 0.25, 2.2), [-16, 4, 10], [20, 7]);
  panel(new Color().setRGB(2.8, 1.45, 0.5), [2, 15, 8], [24, 10]);
  panel(new Color().setRGB(0.5, 1.0, 3.0), [4, 2, -22], [20, 6]);

  const generator = new PMREMGenerator(renderer);
  generator.compileEquirectangularShader();
  const target = generator.fromScene(envScene, 0.02, 0.1, 100);
  generator.dispose();

  sky.geometry.dispose();
  sky.material.dispose();
  envScene.traverse((child) => {
    if (child instanceof Mesh && child !== sky) {
      child.geometry.dispose();
      if (Array.isArray(child.material)) {
        child.material.forEach((material) => {
          material.dispose();
        });
      } else {
        child.material.dispose();
      }
    }
  });

  return target.texture;
}
