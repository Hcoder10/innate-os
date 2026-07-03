// Ambient WebSerial types (Chromium-only; not part of TypeScript's dom lib).
// Mirrors webapp/js/types.d.ts's hand-rolled subset -- only what dynamixel.ts
// actually uses.

interface SerialPort extends EventTarget {
  readonly readable: ReadableStream<Uint8Array> | null;
  readonly writable: WritableStream<Uint8Array> | null;
  open(options: { baudRate: number; bufferSize?: number }): Promise<void>;
  close(): Promise<void>;
}

interface Serial extends EventTarget {
  getPorts(): Promise<SerialPort[]>;
  requestPort(options?: object): Promise<SerialPort>;
}

interface Navigator {
  readonly serial?: Serial;
}
