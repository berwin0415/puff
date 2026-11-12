import type { GameSnapshot, QualityLevel } from './engine/types';
import { formatTime } from './engine/utils';
import styles from './NeonVeil.module.css';

interface StatusScreenProps {
  mode: 'paused' | 'dead' | 'victory';
  snapshot: GameSnapshot;
  quality: QualityLevel;
  muted: boolean;
  onResume: () => void;
  onRestart: () => void;
  onQualityChange: (quality: QualityLevel) => void;
  onToggleMute: () => void;
}

export function StatusScreen({
  mode,
  snapshot,
  quality,
  muted,
  onResume,
  onRestart,
  onQualityChange,
  onToggleMute,
}: StatusScreenProps) {
  const isPaused = mode === 'paused';
  const title = isPaused ? '链路中断' : mode === 'dead' ? '行动失败' : '区域肃清';
  const kicker = isPaused ? 'SYSTEM PAUSED' : mode === 'dead' ? 'OPERATOR DOWN' : 'SECTOR SECURED';
  const description = isPaused
    ? '战术链路已挂起。确认装备后继续行动。'
    : mode === 'dead'
      ? '生命信号消失。第七区防线失守。'
      : '所有敌对信号已清除。黎明前的雨仍未停。';

  return (
    <section className={`${styles.statusScreen} ${isPaused ? styles.statusPaused : ''}`}>
      <div className={styles.statusPanel}>
        <div className={styles.statusKicker}>
          <i />
          <span>{kicker}</span>
          <i />
        </div>
        <h2
          className={
            mode === 'dead' ? styles.statusDead : mode === 'victory' ? styles.statusVictory : ''
          }
        >
          {title}
        </h2>
        <p className={styles.statusDescription}>{description}</p>

        {!isPaused && (
          <div className={styles.resultGrid}>
            <div>
              <span>最终得分</span>
              <b>{snapshot.score.toLocaleString('en-US')}</b>
            </div>
            <div>
              <span>抵达波次</span>
              <b>
                {snapshot.wave} / {snapshot.totalWaves}
              </b>
            </div>
            <div>
              <span>击杀 / 爆头</span>
              <b>
                {snapshot.kills} / {snapshot.headshots}
              </b>
            </div>
            <div>
              <span>命中率</span>
              <b>{snapshot.accuracy}%</b>
            </div>
            <div>
              <span>行动时长</span>
              <b>{formatTime(snapshot.elapsed)}</b>
            </div>
            <div>
              <span>威胁清除</span>
              <b>{snapshot.kills * 100}</b>
            </div>
          </div>
        )}

        <div className={styles.statusActions}>
          {isPaused ? (
            <>
              <button type="button" className={styles.deployButton} onClick={onResume}>
                <span>继续行动</span>
                <small>RESUME</small>
              </button>
              <button type="button" className={styles.ghostButton} onClick={onRestart}>
                重新部署
              </button>
            </>
          ) : (
            <button type="button" className={styles.deployButton} onClick={onRestart}>
              <span>{mode === 'dead' ? '再次部署' : '重新开始'}</span>
              <small>REDEPLOY</small>
            </button>
          )}
        </div>

        {isPaused && (
          <div className={styles.pauseOptions}>
            <div className={styles.qualityPicker}>
              <span>视觉预设</span>
              <div>
                <button
                  type="button"
                  className={quality === 'cinematic' ? styles.qualityActive : ''}
                  onClick={() => onQualityChange('cinematic')}
                >
                  电影级
                </button>
                <button
                  type="button"
                  className={quality === 'performance' ? styles.qualityActive : ''}
                  onClick={() => onQualityChange('performance')}
                >
                  性能
                </button>
              </div>
            </div>
            <button type="button" className={styles.muteButton} onClick={onToggleMute}>
              {muted ? '开启声音' : '关闭声音'}
            </button>
          </div>
        )}

        <p className={styles.statusFoot}>
          {isPaused
            ? 'WASD 移动 · 鼠标瞄准 · ESC 暂停 · R 换弹'
            : '每一次阵亡都会重新校准你的战术链路。'}
        </p>
      </div>
    </section>
  );
}
