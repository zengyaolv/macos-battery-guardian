import Cocoa
import IOKit.ps

// Arguments:
// 1: mode ("high" | "low" | "critical")
// 2: percentage (e.g. "75")
// 3: detail message (optional)
// 4: device name (optional, default "MacBook")
// 5: state.json path (optional, for writing pause_until_ts when clicking Pause 2h)
// 6: dismissPolicy ("auto" | "change", default "auto")

final class PowerWatcher {
    private var runLoopSource: CFRunLoopSource?
    private var pollTimer: Timer?
    private let onChange: () -> Void

    init(onChange: @escaping () -> Void) {
        self.onChange = onChange
    }

    static func isDrawingFromACPower() -> Bool {
        guard let blob = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let sourceType = IOPSGetProvidingPowerSourceType(blob)?.takeRetainedValue() as String? else {
            return false
        }
        return sourceType == (kIOPMACPowerKey as String)
    }

    func start() {
        let context = UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())
        if let src = IOPSNotificationCreateRunLoopSource({ ctx in
            guard let ctx = ctx else { return }
            let watcher = Unmanaged<PowerWatcher>.fromOpaque(ctx).takeUnretainedValue()
            DispatchQueue.main.async {
                watcher.onChange()
            }
        }, context)?.takeRetainedValue() {
            self.runLoopSource = src
            CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        }
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.onChange()
        }
    }

    func stop() {
        pollTimer?.invalidate()
        pollTimer = nil
        if let src = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), src, .commonModes)
            runLoopSource = nil
        }
    }

    deinit {
        stop()
    }
}

class AlertOverlayApp: NSObject, NSApplicationDelegate {
    var windows: [NSWindow] = []
    let mode: String
    let percent: String
    let customMessage: String
    let deviceName: String
    let statePath: String
    let dismissPolicy: String
    let initialOnAC: Bool
    var powerWatcher: PowerWatcher?

    init(
        mode: String,
        percent: String,
        customMessage: String,
        deviceName: String,
        statePath: String,
        dismissPolicy: String
    ) {
        self.mode = mode
        self.percent = percent
        self.customMessage = customMessage
        self.deviceName = deviceName
        self.statePath = statePath
        self.dismissPolicy = dismissPolicy
        self.initialOnAC = PowerWatcher.isDrawingFromACPower()
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        createFullScreenWindows()
        startPowerWatcher()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        powerWatcher?.stop()
    }

    func startPowerWatcher() {
        let watcher = PowerWatcher { [weak self] in
            self?.checkPowerAndDismissIfNeeded()
        }
        self.powerWatcher = watcher
        watcher.start()
        checkPowerAndDismissIfNeeded()
    }

    func checkPowerAndDismissIfNeeded() {
        let nowOnAC = PowerWatcher.isDrawingFromACPower()
        let shouldDismiss: Bool
        if dismissPolicy == "change" {
            shouldDismiss = (nowOnAC != initialOnAC)
        } else {
            if mode == "high" {
                shouldDismiss = !nowOnAC
            } else {
                // "low" or "critical"
                shouldDismiss = nowOnAC
            }
        }
        if shouldDismiss {
            powerWatcher?.stop()
            NSApp.terminate(nil)
        }
    }

    func createFullScreenWindows() {
        let screens = NSScreen.screens
        for (idx, screen) in screens.enumerated() {
            let window = KeyableWindow(
                contentRect: screen.frame,
                styleMask: [.borderless],
                backing: .buffered,
                defer: false
            )
            window.level = .screenSaver
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            window.isOpaque = false
            window.backgroundColor = .clear
            window.hasShadow = false

            let contentView = buildContentView(frame: screen.frame, isPrimary: idx == 0)
            window.contentView = contentView
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
            windows.append(window)
        }
    }

    func buildContentView(frame: NSRect, isPrimary: Bool) -> NSView {
        let rootView = NSView(frame: NSRect(origin: .zero, size: frame.size))
        rootView.wantsLayer = true

        let bgColor: NSColor
        let accentColor: NSColor
        let badgeText: String
        let headlineText: String
        let subText: String
        let iconSymbol: String

        if mode == "high" {
            bgColor = NSColor(calibratedRed: 0.04, green: 0.18, blue: 0.12, alpha: 0.92)
            accentColor = NSColor(calibratedRed: 0.20, green: 0.88, blue: 0.52, alpha: 1.0)
            iconSymbol = "⚡"
            badgeText = "[\(deviceName)] HIGH BATTERY ALERT (> 70% · SILENT FULLSCREEN)"
            headlineText = "\(deviceName) at \(percent)%"
            subText = customMessage.isEmpty
                ? "Battery is above 70% on AC power. Unplug your charger to auto-close this alert and preserve battery health."
                : customMessage
        } else if mode == "critical" {
            bgColor = NSColor(calibratedRed: 0.28, green: 0.03, blue: 0.05, alpha: 0.95)
            accentColor = NSColor(calibratedRed: 1.0, green: 0.25, blue: 0.25, alpha: 1.0)
            iconSymbol = "🚨"
            badgeText = "[\(deviceName)] CRITICAL BATTERY ALERT (< 15% · SILENT FULLSCREEN)"
            headlineText = "\(deviceName) Only \(percent)% Left"
            subText = customMessage.isEmpty
                ? "Battery has stayed below 15% unplugged! An emergency email alert was sent. Plug in AC power immediately to auto-close."
                : customMessage
        } else {
            bgColor = NSColor(calibratedRed: 0.22, green: 0.05, blue: 0.06, alpha: 0.93)
            accentColor = NSColor(calibratedRed: 1.0, green: 0.38, blue: 0.22, alpha: 1.0)
            iconSymbol = "🪫"
            badgeText = "[\(deviceName)] LOW BATTERY ALERT (< 30% · SILENT FULLSCREEN)"
            headlineText = "\(deviceName) at \(percent)%"
            subText = customMessage.isEmpty
                ? "Battery has dropped below 30%. Plug in your charger to auto-close this alert."
                : customMessage
        }

        rootView.layer?.backgroundColor = bgColor.cgColor

        // Center card
        let cardWidth: CGFloat = min(840, frame.width * 0.85)
        let cardHeight: CGFloat = 500
        let cardFrame = NSRect(
            x: (frame.width - cardWidth) / 2,
            y: (frame.height - cardHeight) / 2,
            width: cardWidth,
            height: cardHeight
        )

        let cardView = NSView(frame: cardFrame)
        cardView.wantsLayer = true
        cardView.layer?.backgroundColor = NSColor(calibratedWhite: 0.08, alpha: 0.90).cgColor
        cardView.layer?.cornerRadius = 28
        cardView.layer?.borderWidth = 3
        cardView.layer?.borderColor = accentColor.cgColor

        // Icon label
        let iconLabel = NSTextField(labelWithString: iconSymbol)
        iconLabel.font = NSFont.systemFont(ofSize: 74)
        iconLabel.alignment = .center
        iconLabel.frame = NSRect(x: 0, y: cardHeight - 120, width: cardWidth, height: 88)
        cardView.addSubview(iconLabel)

        // Badge label
        let badgeLabel = NSTextField(labelWithString: badgeText)
        badgeLabel.font = NSFont.boldSystemFont(ofSize: 20)
        badgeLabel.textColor = accentColor
        badgeLabel.alignment = .center
        badgeLabel.frame = NSRect(x: 20, y: cardHeight - 165, width: cardWidth - 40, height: 34)
        cardView.addSubview(badgeLabel)

        // Giant percentage headline
        let titleLabel = NSTextField(labelWithString: headlineText)
        titleLabel.font = NSFont.systemFont(ofSize: 58, weight: .heavy)
        titleLabel.textColor = .white
        titleLabel.alignment = .center
        titleLabel.frame = NSRect(x: 20, y: cardHeight - 250, width: cardWidth - 40, height: 78)
        cardView.addSubview(titleLabel)

        // Subtitle description
        let descLabel = NSTextField(wrappingLabelWithString: subText)
        descLabel.font = NSFont.systemFont(ofSize: 20, weight: .medium)
        descLabel.textColor = NSColor(calibratedWhite: 0.9, alpha: 1.0)
        descLabel.alignment = .center
        descLabel.frame = NSRect(x: 48, y: 170, width: cardWidth - 96, height: 68)
        cardView.addSubview(descLabel)

        // Primary dismiss button
        let btnWidth: CGFloat = 420
        let btnHeight: CGFloat = 56
        let dismissBtn = NSButton(frame: NSRect(
            x: (cardWidth - btnWidth) / 2,
            y: 96,
            width: btnWidth,
            height: btnHeight
        ))
        dismissBtn.title = "Dismiss (Esc / Enter / Space)"
        dismissBtn.bezelStyle = .regularSquare
        dismissBtn.isBordered = false
        dismissBtn.wantsLayer = true
        dismissBtn.layer?.backgroundColor = accentColor.cgColor
        dismissBtn.layer?.cornerRadius = 14
        dismissBtn.font = NSFont.boldSystemFont(ofSize: 20)
        dismissBtn.contentTintColor = NSColor.black
        dismissBtn.target = self
        dismissBtn.action = #selector(dismissAlert)
        dismissBtn.keyEquivalent = "\r"
        cardView.addSubview(dismissBtn)

        // Secondary button: Pause 2h
        let subBtnWidth: CGFloat = 380
        let subBtnHeight: CGFloat = 42
        let pause2hBtn = NSButton(frame: NSRect(
            x: (cardWidth - subBtnWidth) / 2,
            y: 34,
            width: subBtnWidth,
            height: subBtnHeight
        ))
        pause2hBtn.title = "Pause Fullscreen Alerts for 2 Hours (Meeting Mode)"
        pause2hBtn.bezelStyle = .regularSquare
        pause2hBtn.isBordered = false
        pause2hBtn.wantsLayer = true
        pause2hBtn.layer?.backgroundColor = NSColor(calibratedWhite: 0.22, alpha: 1.0).cgColor
        pause2hBtn.layer?.cornerRadius = 10
        pause2hBtn.font = NSFont.systemFont(ofSize: 15, weight: .semibold)
        pause2hBtn.contentTintColor = NSColor.white
        pause2hBtn.target = self
        pause2hBtn.action = #selector(pauseTwoHours)
        cardView.addSubview(pause2hBtn)

        rootView.addSubview(cardView)
        return rootView
    }

    @objc func dismissAlert() {
        powerWatcher?.stop()
        NSApp.terminate(nil)
    }

    @objc func pauseTwoHours() {
        powerWatcher?.stop()
        if !statePath.isEmpty {
            let url = URL(fileURLWithPath: statePath)
            var dict: [String: Any] = [:]
            if let data = try? Data(contentsOf: url),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                dict = obj
            }
            dict["pause_until_ts"] = Date().timeIntervalSince1970 + 7200.0
            if let outData = try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted]) {
                try? outData.write(to: url)
            }
        }
        NSApp.terminate(nil)
    }
}

class KeyableWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    override func keyDown(with event: NSEvent) {
        // Esc (53), Space (49), Return (36)
        if event.keyCode == 53 || event.keyCode == 49 || event.keyCode == 36 {
            NSApp.terminate(nil)
        } else {
            super.keyDown(with: event)
        }
    }
}

let args = CommandLine.arguments
let mode = args.count > 1 ? args[1] : "low"
let percent = args.count > 2 ? args[2] : "25"
let customMsg = args.count > 3 ? args[3] : ""
let deviceName = args.count > 4 ? args[4] : "MacBook"
let statePath = args.count > 5 ? args[5] : ""
let dismissPolicy = args.count > 6 ? args[6] : "auto"

let app = NSApplication.shared
let delegate = AlertOverlayApp(
    mode: mode,
    percent: percent,
    customMessage: customMsg,
    deviceName: deviceName,
    statePath: statePath,
    dismissPolicy: dismissPolicy
)
app.delegate = delegate
app.run()
