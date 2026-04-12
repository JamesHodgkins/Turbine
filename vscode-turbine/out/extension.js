"use strict";
/**
 * Turbine VS Code Extension
 *
 * Registers a sidebar WebviewView (like Copilot).  The panel HTML contains
 * a prompt input at the bottom and a scrolling chat log above.  When the
 * user submits a request the WebView posts {command:"run", request, dryRun}
 * back to the extension, which spawns the Python CLI and streams JSON events
 * back into the panel.
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const cp = __importStar(require("child_process"));
const path = __importStar(require("path"));
const fs = __importStar(require("fs"));
// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------
function activate(context) {
    const provider = new TurbineViewProvider(context);
    context.subscriptions.push(vscode.window.registerWebviewViewProvider(TurbineViewProvider.VIEW_ID, provider, { webviewOptions: { retainContextWhenHidden: true } }), 
    // Keep command-palette shortcuts working too
    vscode.commands.registerCommand("turbine.run", () => provider.submitFromCommand(false)), vscode.commands.registerCommand("turbine.runDryRun", () => provider.submitFromCommand(true)));
}
function deactivate() { }
// ---------------------------------------------------------------------------
// WebviewViewProvider
// ---------------------------------------------------------------------------
class TurbineViewProvider {
    constructor(_context) {
        this._context = _context;
    }
    resolveWebviewView(webviewView, _resolverContext, _token) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._context.extensionUri, "media"),
            ],
        };
        webviewView.webview.html = this._buildHtml(webviewView.webview);
        // Handle messages from the WebView
        webviewView.webview.onDidReceiveMessage((msg) => {
            if (msg.command === "run") {
                this._runTurbine(msg.request, msg.dryRun ?? false, msg.newChat ?? false);
            }
            else if (msg.command === "cancel") {
                this._cancel();
            }
            else if (msg.command === "ready") {
                // Webview finished loading — send persisted sessions so the dropdown can populate
                const sessions = this._context.globalState.get("turbine.sessions", []);
                this._post({ event: "_turbine_init", sessions });
            }
            else if (msg.command === "saveSessions") {
                const sessions = msg.sessions;
                if (Array.isArray(sessions)) {
                    this._context.globalState.update("turbine.sessions", sessions.slice(0, 30));
                }
            }
        });
    }
    // Called from command-palette shortcuts — shows an input box then delegates
    async submitFromCommand(dryRun) {
        const request = await vscode.window.showInputBox({
            prompt: "Describe the change you want Turbine to make",
            placeHolder: "e.g. Add input validation to the login form",
            ignoreFocusOut: true,
        });
        if (request) {
            // Reveal the sidebar panel first
            await vscode.commands.executeCommand(`${TurbineViewProvider.VIEW_ID}.focus`);
            this._runTurbine(request, dryRun, false);
        }
    }
    // ---------------------------------------------------------------------------
    // Subprocess management
    // ---------------------------------------------------------------------------
    _cancel() {
        if (this._activeProc) {
            this._activeProc.kill();
            this._activeProc = undefined;
            this._post({ event: "log", level: "error", message: "Cancelled by user." });
            this._post({ event: "_turbine_exit", code: -1 });
        }
    }
    async _runTurbine(request, dryRun, newChat) {
        const config = vscode.workspace.getConfiguration("turbine");
        const pythonPath = config.get("pythonPath", "");
        const extraArgs = config.get("extraArgs", []);
        // Resolve python + project root together.
        // If turbine.pythonPath is explicitly set, use it and use the active
        // workspace folder as target.
        // Otherwise walk up from the extension directory to find a .venv — the
        // folder containing that .venv IS the project root (target).
        let cmd;
        let baseArgs;
        let target;
        const pythonPathExplicit = pythonPath && pythonPath !== "turbine";
        if (pythonPathExplicit) {
            const parts = pythonPath.split(" ");
            cmd = parts[0];
            baseArgs = parts.slice(1);
            // For explicit pythonPath, let the user pick the target folder
            const folders = vscode.workspace.workspaceFolders;
            if (!folders || folders.length === 0) {
                vscode.window.showErrorMessage("Turbine: No workspace folder is open.");
                return;
            }
            if (folders.length === 1) {
                target = folders[0].uri.fsPath;
            }
            else {
                const picked = await vscode.window.showQuickPick(folders.map(f => ({ label: f.name, description: f.uri.fsPath, folder: f })), { placeHolder: "Which folder should Turbine run on?" });
                if (!picked) {
                    return;
                }
                target = picked.folder.uri.fsPath;
            }
        }
        else {
            // Walk up from the extension directory to find a .venv.
            // The directory that contains the .venv becomes the target.
            let resolvedPython = "";
            let resolvedRoot = "";
            let dir = this._context.extensionUri.fsPath;
            for (let i = 0; i < 6; i++) {
                const win = path.join(dir, ".venv", "Scripts", "python.exe");
                const unix = path.join(dir, ".venv", "bin", "python");
                if (fs.existsSync(win)) {
                    resolvedPython = win;
                    resolvedRoot = dir;
                    break;
                }
                if (fs.existsSync(unix)) {
                    resolvedPython = unix;
                    resolvedRoot = dir;
                    break;
                }
                const parent = path.dirname(dir);
                if (parent === dir) {
                    break;
                }
                dir = parent;
            }
            if (resolvedPython) {
                cmd = resolvedPython;
                baseArgs = ["-m", "turbine"];
                // resolvedRoot is where the .venv lives (Turbine's own dir) — use it
                // only for the Python executable, NOT as the project target.
                const folders = vscode.workspace.workspaceFolders;
                if (!folders || folders.length === 0) {
                    vscode.window.showErrorMessage("Turbine: No workspace folder is open.");
                    return;
                }
                if (folders.length === 1) {
                    target = folders[0].uri.fsPath;
                }
                else {
                    const picked = await vscode.window.showQuickPick(folders.map(f => ({ label: f.name, description: f.uri.fsPath, folder: f })), { placeHolder: "Which folder should Turbine run on?" });
                    if (!picked) {
                        return;
                    }
                    target = picked.folder.uri.fsPath;
                }
            }
            else {
                // Last resort: bare turbine on PATH, first workspace folder as target
                cmd = "turbine";
                baseArgs = [];
                const folders = vscode.workspace.workspaceFolders;
                if (!folders || folders.length === 0) {
                    vscode.window.showErrorMessage("Turbine: No workspace folder open and no .venv found.");
                    return;
                }
                target = folders[0].uri.fsPath;
            }
        }
        const args = [
            ...baseArgs,
            target,
            request,
            "--json-events",
            "--no-ui",
            ...(dryRun ? ["--dry-run"] : []),
            ...(newChat ? ["--new-chat"] : []),
            ...extraArgs,
        ];
        // Kill any in-flight run
        if (this._activeProc) {
            this._activeProc.kill();
        }
        this._post({ event: "_turbine_start", target, request, dryRun });
        // Diagnostic — visible in the panel log
        this._post({ event: "log", level: "debug", message: `extensionUri: ${this._context.extensionUri.fsPath}` });
        this._post({ event: "log", level: "debug", message: `target: ${target}` });
        this._post({ event: "log", level: "debug", message: `cmd: ${cmd}` });
        this._post({ event: "log", level: "debug", message: `args: ${args.join(" ")}` });
        const proc = cp.spawn(cmd, args, {
            cwd: target,
            env: {
                ...process.env,
                PYTHONUTF8: "1", // Python 3.7+ UTF-8 mode
                PYTHONIOENCODING: "utf-8", // fallback for older pip/setuptools shims
            },
            shell: false, // never use shell — args are passed as an array, no re-parsing
        });
        this._activeProc = proc;
        let buffer = "";
        proc.stdout.on("data", (chunk) => {
            buffer += chunk.toString("utf8");
            let idx;
            while ((idx = buffer.indexOf("\n")) !== -1) {
                const line = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 1);
                if (!line) {
                    continue;
                }
                try {
                    this._post(JSON.parse(line));
                }
                catch {
                    this._post({ event: "log", level: "debug", message: line });
                }
            }
        });
        proc.stderr.on("data", (chunk) => {
            const text = chunk.toString("utf8").trim();
            if (text) {
                this._post({ event: "log", level: "error", message: text });
            }
        });
        proc.on("close", (code) => {
            if (buffer.trim()) {
                this._post({ event: "log", level: "debug", message: buffer.trim() });
            }
            this._post({ event: "_turbine_exit", code: code ?? -1 });
            this._activeProc = undefined;
        });
        proc.on("error", (err) => {
            vscode.window.showErrorMessage(`Turbine: Failed to start process — ${err.message}`);
            this._post({ event: "log", level: "error", message: `Failed to start: ${err.message}` });
        });
    }
    _post(event) {
        this._view?.webview.postMessage(event);
    }
    // ---------------------------------------------------------------------------
    // HTML
    // ---------------------------------------------------------------------------
    _buildHtml(webview) {
        const mediaPath = vscode.Uri.joinPath(this._context.extensionUri, "media", "panel.html");
        const nonce = generateNonce();
        let html = fs.readFileSync(mediaPath.fsPath, "utf8");
        html = html.replace(/\{\{NONCE\}\}/g, nonce);
        // Rewrite any media URIs so the webview can load local resources
        const mediaBase = webview.asWebviewUri(vscode.Uri.joinPath(this._context.extensionUri, "media")).toString();
        html = html.replace(/\{\{MEDIA_BASE\}\}/g, mediaBase);
        return html;
    }
}
TurbineViewProvider.VIEW_ID = "turbine.sidebarView";
function generateNonce() {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let nonce = "";
    for (let i = 0; i < 32; i++) {
        nonce += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return nonce;
}
//# sourceMappingURL=extension.js.map