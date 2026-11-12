import { Vector3 } from 'three';

export const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value));

export const lerp = (from: number, to: number, amount: number) => from + (to - from) * amount;

export const damp = (from: number, to: number, lambda: number, dt: number) =>
  lerp(from, to, 1 - Math.exp(-lambda * dt));

export const smoothstep = (edge0: number, edge1: number, value: number) => {
  const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return t * t * (3 - 2 * t);
};

export const randomRange = (min: number, max: number) => min + Math.random() * (max - min);

export const randomInt = (min: number, max: number) => Math.floor(randomRange(min, max + 1));

export const pick = <T>(items: readonly T[]): T => items[Math.floor(Math.random() * items.length)];

export function mulberry32(seed: number) {
  let state = seed >>> 0;
  return () => {
    state += 0x6d2b79f5;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 沿射线求与球体的最近交点，返回距离；未命中返回 null。 */
export function raySphereDistance(
  origin: Vector3,
  direction: Vector3,
  center: Vector3,
  radius: number,
): number | null {
  const offset = new Vector3().subVectors(center, origin);
  const projected = offset.dot(direction);
  if (projected < 0) {
    return null;
  }

  const distanceSq = offset.lengthSq() - projected * projected;
  const radiusSq = radius * radius;
  if (distanceSq > radiusSq) {
    return null;
  }

  const near = projected - Math.sqrt(radiusSq - distanceSq);
  return near >= 0 ? near : projected + Math.sqrt(radiusSq - distanceSq);
}

export function formatTime(seconds: number) {
  const mins = Math.floor(seconds / 60)
    .toString()
    .padStart(2, '0');
  const secs = Math.floor(seconds % 60)
    .toString()
    .padStart(2, '0');
  return `${mins}:${secs}`;
}
