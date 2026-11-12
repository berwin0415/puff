export class InputController {
  readonly keys = new Set<string>();
  private readonly pressed = new Set<string>();
  private readonly buttons = new Set<number>();
  private deltaX = 0;
  private deltaY = 0;
  private readonly canvas: HTMLCanvasElement;

  locked = false;
  onLockChange: ((locked: boolean) => void) | null = null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    window.addEventListener('keydown', this.handleKeyDown, { passive: false });
    window.addEventListener('keyup', this.handleKeyUp);
    window.addEventListener('blur', this.handleBlur);
    document.addEventListener('mousemove', this.handleMouseMove);
    document.addEventListener('mousedown', this.handleMouseDown);
    document.addEventListener('mouseup', this.handleMouseUp);
    document.addEventListener('pointerlockchange', this.handlePointerLockChange);
    document.addEventListener('contextmenu', this.handleContextMenu);
  }

  private handleKeyDown = (event: KeyboardEvent) => {
    if (event.code === 'Space' || event.code === 'Tab') {
      event.preventDefault();
    }
    if (!this.keys.has(event.code)) {
      this.pressed.add(event.code);
    }
    this.keys.add(event.code);
  };

  private handleKeyUp = (event: KeyboardEvent) => {
    this.keys.delete(event.code);
  };

  private handleBlur = () => {
    this.keys.clear();
    this.buttons.clear();
  };

  private handleMouseMove = (event: MouseEvent) => {
    if (!this.locked) {
      return;
    }
    this.deltaX += event.movementX;
    this.deltaY += event.movementY;
  };

  private handleMouseDown = (event: MouseEvent) => {
    if (!this.locked) {
      return;
    }
    this.buttons.add(event.button);
  };

  private handleMouseUp = (event: MouseEvent) => {
    this.buttons.delete(event.button);
  };

  private handlePointerLockChange = () => {
    this.locked = document.pointerLockElement === this.canvas;
    if (!this.locked) {
      this.buttons.clear();
    }
    this.onLockChange?.(this.locked);
  };

  private handleContextMenu = (event: MouseEvent) => {
    if (this.locked) {
      event.preventDefault();
    }
  };

  requestLock() {
    if (this.locked) {
      return;
    }
    try {
      const result = this.canvas.requestPointerLock({
        unadjustedMovement: true,
      }) as unknown as Promise<void> | undefined;
      if (result && typeof result.catch === 'function') {
        result.catch(() => {
          const retry = this.canvas.requestPointerLock() as unknown as Promise<void> | undefined;
          retry?.catch(() => undefined);
        });
      }
    } catch {
      this.canvas.requestPointerLock();
    }
  }

  exitLock() {
    if (document.pointerLockElement === this.canvas) {
      document.exitPointerLock();
    }
  }

  consumeMouseDelta() {
    const delta = { x: this.deltaX, y: this.deltaY };
    this.deltaX = 0;
    this.deltaY = 0;
    return delta;
  }

  isDown(code: string) {
    return this.keys.has(code);
  }

  wasPressed(code: string) {
    return this.pressed.has(code);
  }

  isMouseDown(button: number) {
    return this.buttons.has(button);
  }

  endFrame() {
    this.pressed.clear();
  }

  dispose() {
    window.removeEventListener('keydown', this.handleKeyDown);
    window.removeEventListener('keyup', this.handleKeyUp);
    window.removeEventListener('blur', this.handleBlur);
    document.removeEventListener('mousemove', this.handleMouseMove);
    document.removeEventListener('mousedown', this.handleMouseDown);
    document.removeEventListener('mouseup', this.handleMouseUp);
    document.removeEventListener('pointerlockchange', this.handlePointerLockChange);
    document.removeEventListener('contextmenu', this.handleContextMenu);
  }
}
