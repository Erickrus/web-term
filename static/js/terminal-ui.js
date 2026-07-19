/**
 * Terminal UI setup.
 * Initializes xterm.js with addons, ResizeObserver, and theme support.
 * Adapted from the TerminalComponent in raw/terminal-component.js.
 */

const TERMINAL_OPTIONS = {
  macOptionIsMeta: true,
  scrollback: 5000,
  screenReaderMode: true,
  cursorBlink: true,
  fontSize: 14,
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', Menlo, monospace",
  allowProposedApi: true,
};

const LIGHT_THEME = {
  background: "#ffffff",
  foreground: "#000000",
  cursor: "rgb(20, 20, 20)",
  selectionBackground: "rgba(0, 0, 0, 0.1)",
};

const DARK_THEME = {
  background: "#1e1e1e",
  foreground: "#ffffff",
  cursor: "rgb(200, 200, 200)",
  selectionBackground: "rgba(255, 255, 255, 0.15)",
};

class TerminalUI {
  constructor(container) {
    this.container = container;
    this.terminal = null;
    this.fitAddon = null;
    this.resizeObserver = null;
  }

  init() {
    const Terminal = window.Terminal;
    const FitAddon = window.FitAddon.FitAddon;
    const Unicode11Addon = window.Unicode11Addon.Unicode11Addon;

    const theme = this._detectTheme() === "dark" ? DARK_THEME : LIGHT_THEME;

    this.terminal = new Terminal({
      ...TERMINAL_OPTIONS,
      theme,
    });

    this.fitAddon = new FitAddon();
    this.terminal.loadAddon(this.fitAddon);

    const unicode11 = new Unicode11Addon();
    this.terminal.loadAddon(unicode11);
    this.terminal.unicode.activeVersion = "11";

    if (window.CanvasAddon) {
      this.terminal.loadAddon(new window.CanvasAddon.CanvasAddon());
    }

    this.terminal.open(this.container);
    this.fitAddon.fit();

    this.resizeObserver = new ResizeObserver(() => {
      const el = this.terminal.element;
      if (el && el.isConnected && el.clientWidth > 0 && el.clientHeight > 0) {
        this.fitAddon.fit();
      }
    });
    this.resizeObserver.observe(this.container);

    return this.terminal;
  }

  fit() {
    if (this.fitAddon) {
      this.fitAddon.fit();
    }
  }

  setTheme(mode) {
    if (!this.terminal) return;
    this.terminal.options.theme = mode === "dark" ? DARK_THEME : LIGHT_THEME;
  }

  dispose() {
    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }
    if (this.terminal) {
      this.terminal.dispose();
      this.terminal = null;
    }
  }

  _detectTheme() {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }
}

export { TerminalUI, TERMINAL_OPTIONS, LIGHT_THEME, DARK_THEME };
