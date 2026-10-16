import Cocoa
import WebKit
import UniformTypeIdentifiers

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var loadingView: NSView!
    var loadingLabel: NSTextField!
    var spinner: NSProgressIndicator!
    var retryButton: NSButton!
    var origin: URL?
    var connected = false
    var starting = false
    var allowClose = false
    var decisionPending = false
    var activeDownloads: [ObjectIdentifier: WKDownload] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenu()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.add(self, name: "tingjiClipboard")
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.underPageBackgroundColor = NSColor(red: 0.973, green: 0.977, blue: 0.969, alpha: 1)

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "听记"
        window.appearance = NSAppearance(named: .aqua)
        window.minSize = NSSize(width: 900, height: 620)
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(red: 0.973, green: 0.977, blue: 0.969, alpha: 1)
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("TingjiWorkbench")
        window.contentView = webView
        buildLoadingView()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startService()
    }

    func buildLoadingView() {
        loadingView = NSView()
        loadingView.translatesAutoresizingMaskIntoConstraints = false
        webView.addSubview(loadingView)
        NSLayoutConstraint.activate([
            loadingView.leadingAnchor.constraint(equalTo: webView.leadingAnchor),
            loadingView.trailingAnchor.constraint(equalTo: webView.trailingAnchor),
            loadingView.topAnchor.constraint(equalTo: webView.topAnchor),
            loadingView.bottomAnchor.constraint(equalTo: webView.bottomAnchor)
        ])
        let title = NSTextField(labelWithString: "听记")
        title.font = .systemFont(ofSize: 32, weight: .semibold)
        title.textColor = NSColor(red: 0.21, green: 0.36, blue: 0.29, alpha: 1)
        loadingLabel = NSTextField(wrappingLabelWithString: "正在准备本地工作台…")
        loadingLabel.alignment = .center
        loadingLabel.textColor = .secondaryLabelColor
        loadingLabel.preferredMaxLayoutWidth = 520
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        retryButton = NSButton(title: "重新连接", target: self, action: #selector(reconnect))
        retryButton.bezelStyle = .rounded
        retryButton.isHidden = true
        let stack = NSStackView(views: [title, loadingLabel, spinner, retryButton])
        stack.orientation = .vertical
        stack.spacing = 20
        stack.translatesAutoresizingMaskIntoConstraints = false
        loadingView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: loadingView.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: loadingView.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: 540)
        ])
    }

    func installMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于听记", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏听记", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出听记", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let file = NSMenuItem(title: "文件", action: nil, keyEquivalent: "")
        let fileMenu = NSMenu(title: "文件")
        file.submenu = fileMenu
        fileMenu.addItem(withTitle: "导入录音…", action: #selector(importRecording), keyEquivalent: "o")
        fileMenu.addItem(withTitle: "保存笔记", action: #selector(saveNotes), keyEquivalent: "s")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        menu.addItem(file)
        let edit = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "编辑")
        edit.submenu = editMenu
        for (title, selector, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        menu.addItem(edit)
        let windowItem = NSMenuItem(title: "窗口", action: nil, keyEquivalent: "")
        let windowMenu = NSMenu(title: "窗口")
        windowItem.submenu = windowMenu
        windowMenu.addItem(withTitle: "显示工作台", action: #selector(showWindow), keyEquivalent: "1")
        windowMenu.addItem(withTitle: "重新连接服务", action: #selector(reconnect), keyEquivalent: "")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        menu.addItem(windowItem)
        NSApp.mainMenu = menu
        NSApp.windowsMenu = windowMenu
    }

    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "听记", .applicationVersion: "2.0", .credits: NSAttributedString(string: "本地录音工作台\n音频、转写和个人笔记保存在这台 Mac。")])
    }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func importRecording() {
        showWindow()
        if connected { webView.evaluateJavaScript("document.getElementById('file-input').click()", completionHandler: nil) }
    }
    @objc func saveNotes() {
        if connected {
            webView.evaluateJavaScript("if(typeof state!=='undefined'&&state.tab==='notes'&&state.detail){saveNotes().catch(e=>toast(e.message,true));}", completionHandler: nil)
        }
    }
    @objc func reconnect() {
        if starting { return }
        confirmLeaving { okay in if okay { self.startService() } }
    }

    func startService() {
        guard !starting else { return }
        starting = true
        connected = false
        loadingView.isHidden = false
        loadingLabel.stringValue = "正在连接本地资料库…"
        retryButton.isHidden = true
        spinner.startAnimation(nil)
        guard let resource = Bundle.main.url(forResource: "Project", withExtension: "plist"),
              let data = try? Data(contentsOf: resource),
              let settings = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: String],
              let root = settings["ProjectPath"] else {
            fail("无法读取项目位置，请重新安装听记应用。")
            return
        }
        let executable = URL(fileURLWithPath: root).appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            fail("找不到本地运行环境。请保留原项目目录；如果已移动项目，请在新位置重新构建听记应用。")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = executable
            process.currentDirectoryURL = URL(fileURLWithPath: root)
            process.arguments = ["-m", "audio_notes.cli", "--config", root + "/config.toml", "workbench", "--url"]
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONPATH"] = root + "/src"
            process.environment = environment
            process.standardInput = FileHandle.nullDevice
            let output = Pipe()
            process.standardOutput = output
            // Diagnostics are bounded by the launcher and kept out of the JSON channel.
            let logURL = URL(fileURLWithPath: root + "/state/logs/native-app.log")
            try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
            let log = try? FileHandle(forWritingTo: logURL)
            _ = try? log?.seekToEnd()
            process.standardError = log ?? FileHandle.nullDevice
            do {
                try process.run()
                DispatchQueue.global().asyncAfter(deadline: .now() + 45) {
                    if process.isRunning { process.terminate() }
                }
                let result = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                try? log?.close()
                guard process.terminationStatus == 0,
                      let record = try JSONSerialization.jsonObject(with: result) as? [String: Any],
                      let address = record["url"] as? String,
                      let token = record["token"] as? String,
                      let url = URL(string: address), url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
                      token.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil,
                      let launchURL = URL(string: address + "/#token=" + token) else {
                    throw NSError(domain: "Tingji", code: 1, userInfo: [NSLocalizedDescriptionKey: "本地服务未能启动。请检查项目位置，或查看 state/logs/native-app.log。"])
                }
                DispatchQueue.main.async {
                    self.origin = url
                    self.starting = false
                    self.webView.load(URLRequest(url: launchURL))
                }
            } catch {
                try? log?.close()
                DispatchQueue.main.async { self.fail(error.localizedDescription) }
            }
        }
    }

    func fail(_ message: String) {
        starting = false
        connected = false
        loadingView.isHidden = false
        spinner.stopAnimation(nil)
        loadingLabel.stringValue = message
        retryButton.isHidden = false
    }
    func isLocal(_ url: URL?) -> Bool {
        guard let url = url, let origin = origin else { return false }
        return url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard isLocal(webView.url) else { return }
        connected = true
        spinner.stopAnimation(nil)
        loadingView.isHidden = true
        window.makeFirstResponder(webView)
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { fail("无法连接本地工作台：" + error.localizedDescription) }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { fail(error.localizedDescription) }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { fail("界面进程已退出。可以重新连接；后台任务和已保存的笔记仍保留。") }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if isLocal(url) {
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        } else {
            decisionHandler(.cancel)
            if action.navigationType == .linkActivated && ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        }
    }
    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(disposition.lowercased().contains("attachment") || !response.canShowMIMEType ? .download : .allow)
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, isLocal(url) { webView.load(action.request) }
        return nil
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { prepare(download) }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { prepare(download) }
    func prepare(_ download: WKDownload) { activeDownloads[ObjectIdentifier(download)] = download; download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.title = "导出文稿"
        panel.nameFieldStringValue = suggestedFilename
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { result in
            if result == .OK { completionHandler(panel.url) }
            else { completionHandler(nil); self.activeDownloads.removeValue(forKey: ObjectIdentifier(download)) }
        }
    }
    func downloadDidFinish(_ download: WKDownload) { activeDownloads.removeValue(forKey: ObjectIdentifier(download)) }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        activeDownloads.removeValue(forKey: ObjectIdentifier(download))
        if (error as NSError).code != NSURLErrorCancelled { showAlert("导出未完成", error.localizedDescription) }
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.title = "选择要导入的录音"
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = "听记"; alert.informativeText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = "确认操作"; alert.informativeText = message
        alert.addButton(withTitle: "继续"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { result in completionHandler(result == .alertFirstButtonReturn) }
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "tingjiClipboard", message.frameInfo.isMainFrame, isLocal(message.frameInfo.request.url), let text = message.body as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
    func showAlert(_ title: String, _ message: String) {
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = message
        alert.beginSheetModal(for: window)
    }
    func confirmLeaving(_ completion: @escaping (Bool) -> Void) {
        guard connected else { completion(true); return }
        webView.evaluateJavaScript("typeof state!=='undefined' && (state.dirty || state.uploading)") { value, error in
            if error != nil { completion(false); self.showAlert("暂时无法确认保存状态", "请稍后再试，避免丢失尚未保存的内容。"); return }
            if value as? Bool != true && self.activeDownloads.isEmpty { completion(true); return }
            let alert = NSAlert()
            alert.messageText = "有尚未保存或传输完成的内容"
            alert.informativeText = "离开可能丢失未保存的笔记或中断文件传输。已提交的后台处理任务会继续运行。"
            alert.addButton(withTitle: "留在工作台")
            alert.addButton(withTitle: "仍然离开")
            alert.beginSheetModal(for: self.window) { result in completion(result == .alertSecondButtonReturn) }
        }
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if allowClose { allowClose = false; return true }
        if decisionPending { return false }
        decisionPending = true
        confirmLeaving { okay in
            self.decisionPending = false
            if okay { self.allowClose = true; sender.performClose(nil) }
        }
        return false
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        confirmLeaving { okay in sender.reply(toApplicationShouldTerminate: okay) }
        return .terminateLater
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
