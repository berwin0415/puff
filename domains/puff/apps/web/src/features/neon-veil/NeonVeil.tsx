import { useCallback, useEffect, useRef, useState } from 'react';
import { GameEngine } from './engine/game-engine';
import {
  EMPTY_SNAPSHOT,
  type GameEvent,
  type GameSnapshot,
  type QualityLevel,
} from './engine/types';
import { GameHud, type HitStamp, type KillFeedItem } from './Hud';
import styles from './NeonVeil.module.css';
import { StartScreen } from './StartScreen';
import { StatusScreen } from './StatusScreen';

interface Banner {
  id: number;
  label: string;
  sub: string;
}

export function NeonVeil() {
  const hostRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<GameEngine | null>(null);
  const timersRef = useRef<Set<number>>(new Set());
  const eventIdRef = useRef(0);
  const [snapshot, setSnapshot] = useState<GameSnapshot>(EMPTY_SNAPSHOT);
  const [quality, setQuality] = useState<QualityLevel>('cinematic');
  const [muted, setMuted] = useState(false);
  const [booted, setBooted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feed, setFeed] = useState<KillFeedItem[]>([]);
  const [banner, setBanner] = useState<Banner | null>(null);
  const [hitStamp, setHitStamp] = useState<HitStamp>({ id: 0, headshot: false });
  const [damageStamp, setDamageStamp] = useState(0);

  const schedule = useCallback((callback: () => void, delay: number) => {
    const timer = window.setTimeout(() => {
      timersRef.current.delete(timer);
      callback();
    }, delay);
    timersRef.current.add(timer);
  }, []);

  const handleEvent = useCallback(
    (event: GameEvent) => {
      if (event.type === 'kill') {
        const id = ++eventIdRef.current;
        setFeed((current) => [
          ...current.slice(-3),
          {
            id,
            label: event.label ?? '目标清除',
            points: event.value,
            headshot: event.label?.includes('爆头'),
          },
        ]);
        schedule(() => {
          setFeed((current) => current.filter((item) => item.id !== id));
        }, 4200);
        setHitStamp({
          id: ++eventIdRef.current,
          headshot: event.label?.includes('爆头') ?? false,
        });
      } else if (event.type === 'hit' || event.type === 'headshot') {
        setHitStamp({ id: ++eventIdRef.current, headshot: event.type === 'headshot' });
      }

      if (event.type === 'wave') {
        const id = ++eventIdRef.current;
        setBanner({
          id,
          label: `第 ${String(event.value ?? 1).padStart(2, '0')} 波 // ${event.label ?? '未知信号'}`,
          sub:
            event.label === '区域暂时安全' ? '补给已投放 · 准备下一波接触' : '敌方机械单位正在接近',
        });
        schedule(() => {
          setBanner((current) => (current?.id === id ? null : current));
        }, 2800);
      }

      if (event.type === 'damage') {
        setDamageStamp(++eventIdRef.current);
      }
    },
    [schedule],
  );

  useEffect(() => {
    const host = hostRef.current;
    if (!host) {
      return;
    }

    let engine: GameEngine | null = null;
    let captureTimer = 0;
    try {
      engine = new GameEngine(
        host,
        {
          onSnapshot: setSnapshot,
          onEvent: handleEvent,
        },
        'cinematic',
      );
      engineRef.current = engine;
      setBooted(true);
      if (new URLSearchParams(window.location.search).has('capture')) {
        captureTimer = window.setTimeout(() => engine?.start(false), 350);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }

    return () => {
      window.clearTimeout(captureTimer);
      for (const timer of timersRef.current) {
        window.clearTimeout(timer);
      }
      timersRef.current.clear();
      engine?.dispose();
      engineRef.current = null;
    };
  }, [handleEvent]);

  useEffect(() => {
    engineRef.current?.setQuality(quality);
  }, [quality]);

  useEffect(() => {
    engineRef.current?.setMuted(muted);
  }, [muted]);

  const handleStart = useCallback(() => {
    setFeed([]);
    setBanner(null);
    setHitStamp({ id: 0, headshot: false });
    setDamageStamp(0);
    engineRef.current?.start();
  }, []);

  const handleResume = useCallback(() => {
    engineRef.current?.resume();
  }, []);

  const handleRestart = useCallback(() => {
    setFeed([]);
    setBanner(null);
    engineRef.current?.restart();
  }, []);

  const handleQualityChange = useCallback((nextQuality: QualityLevel) => {
    setQuality(nextQuality);
  }, []);

  const phase = snapshot.phase;
  const statusMode =
    phase === 'paused'
      ? 'paused'
      : phase === 'dead'
        ? 'dead'
        : phase === 'victory'
          ? 'victory'
          : null;

  return (
    <main className={`${styles.stage} ${booted ? styles.stageReady : ''}`}>
      <div
        ref={hostRef}
        className={`${styles.canvasHost} ${phase === 'briefing' ? styles.canvasBriefing : ''}`}
      />

      {phase !== 'briefing' && (
        <GameHud
          snapshot={snapshot}
          feed={feed}
          hitStamp={hitStamp}
          damageStamp={damageStamp}
          banner={banner}
          muted={muted}
          onToggleMute={() => setMuted((value) => !value)}
        />
      )}

      {phase === 'briefing' && (
        <StartScreen
          quality={quality}
          muted={muted}
          onStart={handleStart}
          onQualityChange={handleQualityChange}
          onToggleMute={() => setMuted((value) => !value)}
        />
      )}

      {statusMode && (
        <StatusScreen
          mode={statusMode}
          snapshot={snapshot}
          quality={quality}
          muted={muted}
          onResume={handleResume}
          onRestart={handleRestart}
          onQualityChange={handleQualityChange}
          onToggleMute={() => setMuted((value) => !value)}
        />
      )}

      {error && (
        <div className={styles.errorScreen}>
          <span>RENDER LINK FAILED</span>
          <h2>无法启动渲染链路</h2>
          <p>{error}</p>
          <small>请确认浏览器已启用 WebGL 2.0 与硬件加速。</small>
        </div>
      )}

      <div className={styles.scanlineOverlay} />
      <div className={styles.grainOverlay} />
      <div className={styles.frameOverlay}>
        <i />
        <i />
        <i />
        <i />
      </div>
    </main>
  );
}
