import Cocoa
import IOKit
import IOKit.ps

// Arguments:
// 1: mode ("high" | "low" | "critical" | "inspect")
// 2: percentage (e.g. "75")
// 3: detail message (optional)
// 4: device name (optional, default "MacBook")
// 5: state.json path (optional, for writing pause_until_ts when clicking Pause 2h)
// 6: dismissPolicy ("auto" | "change" | "manual", default "auto")

struct BatteryHUDTelemetry {
    var osPercent: Int?
    var rawSocPct: Double?
    var rawCurrentMah: Int?
    var rawMaxMah: Int?
    var nominalMah: Int?
    var designMah: Int?
    var trueHealthPct: Double?
    var cycleCount: Int?
    var designCycles: Int = 1000
    var gaugeModel: String = "AppleSmartBattery"
    var maskedSerial: String = "N/A"
    var voltageMv: Int?
    var amperageMa: Int?
    var isCharging: Bool = false
    var externalConnected: Bool = false
    var timeRemainingMin: Int?
    var batteryPowerW: Double?
    var systemLoadW: Double?
    var wallInW: Double?
    var adapterWatts: Int?
    var adapterDesc: String = ""
    var instRatePctH: Double?
    var tempC: Double?
    var minTempC: Int?
    var maxTempC: Int?
    var avgTempC: Double?
    var cellVoltagesMv: [Int] = []
    var cellDeltaMv: Int?
    var qmaxMah: [Int] = []
    var qmaxDeltaMah: Int?
    var weightedRaMohm: [Int] = []
    var dod0: [Int] = []
    var cycleLastQmax: Int?
    var tempSamples: Int?
    var operatingHours: Int?
    var impliedHours: Double?
    var clockDiffPct: Double?
    var flashWrites: Int?
    var pfStatus: Int?
    var disconnectCount: Int?
    var forensicsVerdict: String = "PARTIAL"
    var forensicsHeadline: String = "Partial Hardware Support"
    var forensicsDetail: String = "Limited gas-gauge registers exposed"
}

enum SmartBatteryReader {
    private static func signedInt(_ value: Any?) -> Int? {
        guard let num = value as? NSNumber else { return nil }
        return Int(Int64(bitPattern: num.uint64Value))
    }

    private static func maskSerial(_ raw: Any?) -> String {
        guard let s = raw as? String else { return "N/A" }
        let cleaned = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if cleaned.count >= 6 {
            let prefix = String(cleaned.prefix(2))
            let suffix = String(cleaned.suffix(2))
            return "\(prefix)****\(suffix)"
        }
        return cleaned.isEmpty ? "N/A" : "****"
    }

    private static func pickInt(_ dicts: [[String: Any]], _ keys: [String]) -> Int? {
        for dict in dicts {
            for key in keys {
                if let iv = signedInt(dict[key]) {
                    return iv
                }
            }
        }
        return nil
    }

    private static func readProperties(_ entry: io_registry_entry_t) -> [String: Any]? {
        var unmanaged: Unmanaged<CFMutableDictionary>?
        let kr = IORegistryEntryCreateCFProperties(entry, &unmanaged, kCFAllocatorDefault, 0)
        guard kr == KERN_SUCCESS, let cfDict = unmanaged?.takeRetainedValue() else {
            return nil
        }
        return cfDict as? [String: Any]
    }

    private static func entryClassName(_ entry: io_registry_entry_t) -> String {
        if let cfClass = IOObjectCopyClass(entry)?.takeRetainedValue() {
            return cfClass as String
        }
        var nameBuf = [CChar](repeating: 0, count: 128)
        if IORegistryEntryGetName(entry, &nameBuf) == KERN_SUCCESS {
            return String(cString: nameBuf)
        }
        return ""
    }

    static func readTelemetry() -> BatteryHUDTelemetry? {
        let matching = IOServiceMatching("AppleSmartBattery")
        let service = IOServiceGetMatchingService(kIOMainPortDefault, matching)
        guard service != IO_OBJECT_NULL else {
            return nil
        }
        defer { IOObjectRelease(service) }

        guard let root = readProperties(service) else {
            return nil
        }

        var packProps: [String: Any]? = nil
        var bankEntries: [(loc: String, props: [String: Any])] = []

        var iter: io_iterator_t = IO_OBJECT_NULL
        let krIter = IORegistryEntryCreateIterator(
            service,
            kIOServicePlane,
            IOOptionBits(kIORegistryIterateRecursively),
            &iter
        )
        if krIter == KERN_SUCCESS {
            defer { IOObjectRelease(iter) }
            while case let child = IOIteratorNext(iter), child != IO_OBJECT_NULL {
                defer { IOObjectRelease(child) }
                let clsName = entryClassName(child)
                if clsName == "AppleSmartBatteryPack" && packProps == nil {
                    packProps = readProperties(child)
                } else if clsName == "AppleSmartBatteryBank" {
                    if let props = readProperties(child) {
                        var locStr = ""
                        if let bankId = signedInt(props["BankID"]) {
                            locStr = "\(bankId)"
                        } else {
                            var locBuf = [CChar](repeating: 0, count: 64)
                            if IORegistryEntryGetLocationInPlane(child, kIOServicePlane, &locBuf) == KERN_SUCCESS {
                                locStr = String(cString: locBuf)
                            }
                        }
                        bankEntries.append((loc: locStr, props: props))
                    }
                }
            }
        }

        bankEntries.sort { lhs, rhs in
            if let li = Int(lhs.loc), let ri = Int(rhs.loc) {
                return li < ri
            }
            return lhs.loc < rhs.loc
        }

        let rootBdata = (root["BatteryData"] as? [String: Any]) ?? [:]
        let packBdata = (packProps?["BatteryData"] as? [String: Any]) ?? [:]
        let capSources = [root, rootBdata, packBdata]

        var t = BatteryHUDTelemetry()
        t.osPercent = signedInt(root["CurrentCapacity"])
        t.rawCurrentMah = pickInt(capSources, ["AppleRawCurrentCapacity"])
        t.rawMaxMah = pickInt(capSources, ["AppleRawMaxCapacity", "FullChargeCapacity"])
        t.nominalMah = pickInt(capSources, ["NominalChargeCapacity"])
        t.designMah = pickInt(capSources, ["DesignCapacity"])

        let healthRef = (t.rawMaxMah ?? 0) > 0 ? t.rawMaxMah : t.nominalMah
        if let href = healthRef, let design = t.designMah, design > 0 {
            t.trueHealthPct = (Double(href) / Double(design)) * 100.0
        }
        if let cur = t.rawCurrentMah, let maxCap = t.rawMaxMah, maxCap > 0 {
            t.rawSocPct = (Double(cur) / Double(maxCap)) * 100.0
        } else if let pct = t.osPercent {
            t.rawSocPct = Double(pct)
        }

        t.cycleCount = pickInt([root, rootBdata, packBdata], ["CycleCount"])
        t.designCycles = pickInt([root, packBdata], ["DesignCycleCount9C"]) ?? 1000
        t.gaugeModel = (root["DeviceName"] as? String)
            ?? (packBdata["DeviceName"] as? String)
            ?? "AppleSmartBattery"
        t.maskedSerial = maskSerial(packBdata["Serial"] ?? rootBdata["Serial"] ?? root["Serial"])

        t.voltageMv = pickInt([root, packBdata], ["AppleRawBatteryVoltage", "Voltage"])
        t.amperageMa = signedInt(root["InstantAmperage"]) ?? signedInt(root["Amperage"])
        t.isCharging = (root["IsCharging"] as? Bool) ?? false
        t.externalConnected = (root["ExternalConnected"] as? Bool) ?? false

        if let tr = signedInt(root["TimeRemaining"]), tr >= 0, tr < 65535 {
            t.timeRemainingMin = tr
        }

        let ptd = (root["PowerTelemetryData"] as? [String: Any]) ?? [:]
        if let battMw = signedInt(ptd["BatteryPower"]) {
            var watts = Double(battMw) / 1000.0
            if let amp = t.amperageMa {
                if amp < 0 && watts > 0 { watts = -watts }
                else if amp > 0 && watts < 0 { watts = -watts }
            }
            t.batteryPowerW = watts
        } else if let mv = t.voltageMv, let ma = t.amperageMa {
            t.batteryPowerW = (Double(mv) * Double(ma)) / 1_000_000.0
        }
        if let sysMw = signedInt(ptd["SystemLoad"]) {
            t.systemLoadW = Double(sysMw) / 1000.0
        }
        if let wallMw = signedInt(ptd["SystemPowerIn"]) {
            t.wallInW = Double(wallMw) / 1000.0
        }

        let adapter = (root["AdapterDetails"] as? [String: Any]) ?? [:]
        t.adapterWatts = signedInt(adapter["Watts"])
        t.adapterDesc = ((adapter["Description"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        let denomMah = (t.rawMaxMah ?? 0) > 0 ? t.rawMaxMah : t.nominalMah
        if let ma = t.amperageMa, let denom = denomMah, denom > 0 {
            t.instRatePctH = (Double(ma) / Double(denom)) * 100.0
        }

        let lifetime = (packBdata["LifetimeData"] as? [String: Any])
            ?? (rootBdata["LifetimeData"] as? [String: Any])
            ?? [:]

        t.minTempC = signedInt(lifetime["MinimumTemperature"])
        t.maxTempC = signedInt(lifetime["MaximumTemperature"])
        if let avgT = signedInt(lifetime["AverageTemperature"]) {
            t.avgTempC = Double(avgT) / 10.0
        }
        if let packTemp = signedInt(packBdata["Temperature"]) {
            t.tempC = Double(packTemp) / 100.0
        } else if let rootTemp = signedInt(root["Temperature"]) {
            let centi = Double(rootTemp) / 100.0
            let kelvin = Double(rootTemp) / 10.0 - 273.15
            if let avgRef = t.avgTempC {
                t.tempC = abs(kelvin - avgRef) < abs(centi - avgRef) ? kelvin : centi
            } else {
                t.tempC = centi
            }
        }

        t.flashWrites = pickInt([packBdata, rootBdata], ["DataFlashWriteCount"])
        t.pfStatus = pickInt([packBdata, rootBdata], ["PermanentFailureStatus"])
        t.disconnectCount = pickInt([packBdata, rootBdata], ["BatteryCellDisconnectCount"])
        t.cycleLastQmax = signedInt(lifetime["CycleCountLastQmax"])
        t.tempSamples = signedInt(lifetime["TemperatureSamples"])
        t.operatingHours = signedInt(lifetime["TotalOperatingTime"])

        if !bankEntries.isEmpty {
            for entry in bankEntries {
                let bdata = (entry.props["BatteryData"] as? [String: Any]) ?? [:]
                if let cv = signedInt(bdata["CellVoltage"]), cv > 0 { t.cellVoltagesMv.append(cv) }
                if let qm = signedInt(bdata["Qmax"]), qm > 0 { t.qmaxMah.append(qm) }
                if let ra = signedInt(bdata["WeightedRa"]) { t.weightedRaMohm.append(ra) }
                if let d0 = signedInt(bdata["DOD0"]) { t.dod0.append(d0) }
            }
        } else {
            if let arr = (packBdata["CellVoltage"] ?? rootBdata["CellVoltage"]) as? [Any] {
                t.cellVoltagesMv = arr.compactMap { signedInt($0) }.filter { $0 > 0 }
            }
            if let arr = (packBdata["Qmax"] ?? rootBdata["Qmax"]) as? [Any] {
                t.qmaxMah = arr.compactMap { signedInt($0) }.filter { $0 > 0 }
            }
            if let arr = (packBdata["WeightedRa"] ?? rootBdata["WeightedRa"]) as? [Any] {
                t.weightedRaMohm = arr.compactMap { signedInt($0) }
            }
            if let arr = (packBdata["DOD0"] ?? rootBdata["DOD0"]) as? [Any] {
                t.dod0 = arr.compactMap { signedInt($0) }
            }
        }

        if let minV = t.cellVoltagesMv.min(), let maxV = t.cellVoltagesMv.max(), t.cellVoltagesMv.count >= 2 {
            t.cellDeltaMv = maxV - minV
        }
        if let minQ = t.qmaxMah.min(), let maxQ = t.qmaxMah.max(), t.qmaxMah.count >= 2 {
            t.qmaxDeltaMah = maxQ - minQ
        }

        // Compute 4-Gate Anti-Spoofing Verdict
        var checkStatuses: [String] = []
        var parts: [String] = []

        if let clq = t.cycleLastQmax, let cyc = t.cycleCount {
            if clq <= cyc {
                checkStatuses.append("PASS")
                parts.append("Calib \(clq)<=\(cyc)")
            } else {
                checkStatuses.append("FAIL")
                parts.append("Calib Anomaly \(clq)>\(cyc)")
            }
        }

        if let ts = t.tempSamples, ts >= 0, let oh = t.operatingHours {
            let implied = (Double(ts) * 225.0) / 3600.0
            t.impliedHours = implied
            if oh > 0 {
                let rel = abs(implied - Double(oh)) / max(1.0, Double(oh))
                t.clockDiffPct = rel * 100.0
                if rel <= 0.15 {
                    checkStatuses.append("PASS")
                } else if rel <= 0.40 {
                    checkStatuses.append("WARN")
                } else {
                    checkStatuses.append("FAIL")
                }
                parts.append(String(format: "Clock %.0fh/%.0fh (%.2f%%)", implied, Double(oh), rel * 100.0))
            } else if ts > 100 {
                checkStatuses.append("FAIL")
                parts.append("OperatingTime=0 Reset")
            }
        }

        if let delta = t.cellDeltaMv {
            var balStatus = "PASS"
            if delta > 120 { balStatus = "FAIL" }
            else if delta > 50 { balStatus = "WARN" }
            if let cyc = t.cycleCount, cyc > 50, t.qmaxDeltaMah == 0, t.qmaxMah.count >= 2, balStatus == "PASS" {
                balStatus = "WARN"
            }
            checkStatuses.append(balStatus)
        }

        if t.pfStatus != nil || t.disconnectCount != nil || t.flashWrites != nil {
            let pf = t.pfStatus ?? 0
            let disc = t.disconnectCount ?? 0
            let fw = t.flashWrites ?? -1
            let virginDod = !t.dod0.isEmpty && t.dod0.allSatisfy { $0 == 16384 }
            if pf != 0 || disc > 0 {
                checkStatuses.append("FAIL")
                parts.append("PF/Disconnect Alert")
            } else if fw == 0 && ((t.cycleCount ?? 999) <= 2 || virginDod) {
                checkStatuses.append("WARN")
                parts.append("Virgin Flash=0")
            } else {
                checkStatuses.append("PASS")
                if fw >= 0 { parts.append("FlashWrites \(fw)") }
            }
        }

        if checkStatuses.isEmpty {
            t.forensicsVerdict = "PARTIAL"
            t.forensicsHeadline = "PARTIAL · Basic IOKit"
            t.forensicsDetail = "Pack/Bank telemetry unavailable on this host"
        } else if checkStatuses.contains("FAIL") {
            t.forensicsVerdict = "ALERT"
            t.forensicsHeadline = "ALERT · Anomaly Found"
            t.forensicsDetail = parts.joined(separator: " · ")
        } else if checkStatuses.contains("WARN") {
            t.forensicsVerdict = "REVIEW"
            t.forensicsHeadline = "REVIEW · Check Flags"
            t.forensicsDetail = parts.joined(separator: " · ")
        } else {
            t.forensicsVerdict = "PASS"
            t.forensicsHeadline = "PASS · Genuine Consistent"
            t.forensicsDetail = parts.joined(separator: " · ")
        }

        return t
    }
}

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

struct TelemetryTileViews {
    let container: NSView
    let titleLabel: NSTextField
    let valueLabel: NSTextField
    let detailLabel: NSTextField
}

struct ScreenCardWidgets {
    let headlineLabel: NSTextField
    let subLabel: NSTextField
    let tiles: [TelemetryTileViews]
}

class AlertOverlayApp: NSObject, NSApplicationDelegate {
    var windows: [NSWindow] = []
    var screenWidgets: [ScreenCardWidgets] = []
    let mode: String
    let percent: String
    let customMessage: String
    let deviceName: String
    let statePath: String
    let dismissPolicy: String
    let initialOnAC: Bool
    var powerWatcher: PowerWatcher?
    var telemetryTimer: Timer?

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
        refreshTelemetryUI()
        startPowerWatcher()
        startTelemetryTimer()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        telemetryTimer?.invalidate()
        telemetryTimer = nil
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

    func startTelemetryTimer() {
        telemetryTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.refreshTelemetryUI()
        }
    }

    func checkPowerAndDismissIfNeeded() {
        if dismissPolicy == "manual" || mode == "inspect" {
            refreshTelemetryUI()
            return
        }
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
            telemetryTimer?.invalidate()
            powerWatcher?.stop()
            NSApp.terminate(nil)
        }
    }

    func refreshTelemetryUI() {
        let tel = SmartBatteryReader.readTelemetry()
        let livePct = tel?.osPercent.map { "\($0)" } ?? percent
        let onAC = tel?.externalConnected ?? PowerWatcher.isDrawingFromACPower()

        for widgets in screenWidgets {
            if mode == "inspect" {
                let healthStr = tel?.trueHealthPct.map { String(format: "%.1f%%", $0) } ?? "--"
                widgets.headlineLabel.stringValue = "\(deviceName) · \(livePct)% · Health \(healthStr)"

                var metaParts: [String] = []
                metaParts.append(onAC ? "AC Power Connected" : "Running on Battery")
                if let watts = tel?.adapterWatts, onAC {
                    let desc = tel?.adapterDesc.isEmpty == false ? " (\(tel!.adapterDesc))" : ""
                    metaParts.append("Adapter \(watts) W\(desc)")
                }
                if let rem = tel?.timeRemainingMin {
                    let hrs = rem / 60
                    let mins = rem % 60
                    metaParts.append(String(format: "%dh %02dm remaining", hrs, mins))
                }
                metaParts.append("Live IOKit 2s refresh · Press Esc / Enter / Space to close")
                widgets.subLabel.stringValue = metaParts.joined(separator: " · ")
            }

            guard let t = tel, widgets.tiles.count == 6 else {
                continue
            }

            // Tile 0: True Health & Capacity
            if let h = t.trueHealthPct, let rmax = t.rawMaxMah, let des = t.designMah {
                widgets.tiles[0].valueLabel.stringValue = String(format: "%.1f%%", h)
                let nomStr = t.nominalMah.map { " · Nom \($0) mAh" } ?? ""
                widgets.tiles[0].detailLabel.stringValue = "\(rmax) / \(des) mAh\(nomStr)"
            } else {
                widgets.tiles[0].valueLabel.stringValue = "N/A"
                widgets.tiles[0].detailLabel.stringValue = "Capacity registers unavailable"
            }

            // Tile 1: Cycles & Gas Gauge
            if let cyc = t.cycleCount {
                widgets.tiles[1].valueLabel.stringValue = "\(cyc) / \(t.designCycles)"
            } else {
                widgets.tiles[1].valueLabel.stringValue = "N/A"
            }
            widgets.tiles[1].detailLabel.stringValue = "Gauge \(t.gaugeModel) · Serial \(t.maskedSerial)"

            // Tile 2: Live Power & Rate
            let pwrStr = t.batteryPowerW.map { String(format: "%+.1f W", $0) } ?? "-- W"
            let rateStr = t.instRatePctH.map { String(format: " (%+.1f%%/h)", $0) } ?? ""
            widgets.tiles[2].valueLabel.stringValue = "\(pwrStr)\(rateStr)"
            let ampStr = t.amperageMa.map { String(format: "%+d mA", $0) } ?? "-- mA"
            let voltStr = t.voltageMv.map { String(format: "%.2f V", Double($0) / 1000.0) } ?? "-- V"
            let sysStr = t.systemLoadW.map { String(format: " · Sys %.1f W", $0) } ?? ""
            widgets.tiles[2].detailLabel.stringValue = "\(ampStr) @ \(voltStr)\(sysStr)"

            // Tile 3: Pack Temperature
            if let temp = t.tempC {
                widgets.tiles[3].valueLabel.stringValue = String(format: "%.1f °C", temp)
            } else {
                widgets.tiles[3].valueLabel.stringValue = "N/A"
            }
            let avgStr = t.avgTempC.map { String(format: "Avg %.1f °C", $0) } ?? "Avg --"
            let minMaxStr = (t.minTempC != nil && t.maxTempC != nil)
                ? " (Min \(t.minTempC!)° / Max \(t.maxTempC!)°)"
                : ""
            widgets.tiles[3].detailLabel.stringValue = "\(avgStr)\(minMaxStr)"

            // Tile 4: Cell Balance & Impedance
            if let delta = t.cellDeltaMv {
                widgets.tiles[4].valueLabel.stringValue = "Δ \(delta) mV"
                let voltsStr = t.cellVoltagesMv.map { "\($0)" }.joined(separator: "/") + " mV"
                let raStr = t.weightedRaMohm.isEmpty
                    ? ""
                    : " · Ra " + t.weightedRaMohm.map { "\($0)" }.joined(separator: "/") + " mΩ"
                widgets.tiles[4].detailLabel.stringValue = "\(voltsStr)\(raStr)"
            } else {
                widgets.tiles[4].valueLabel.stringValue = "Single / N/A"
                widgets.tiles[4].detailLabel.stringValue = "Per-bank cell voltages unavailable"
            }

            // Tile 5: Anti-Spoofing Forensics
            widgets.tiles[5].valueLabel.stringValue = t.forensicsHeadline
            widgets.tiles[5].detailLabel.stringValue = t.forensicsDetail
            let verdictColor: NSColor
            switch t.forensicsVerdict {
            case "PASS":
                verdictColor = NSColor(calibratedRed: 0.25, green: 0.90, blue: 0.55, alpha: 1.0)
            case "REVIEW":
                verdictColor = NSColor(calibratedRed: 0.98, green: 0.78, blue: 0.25, alpha: 1.0)
            case "ALERT":
                verdictColor = NSColor(calibratedRed: 1.0, green: 0.30, blue: 0.30, alpha: 1.0)
            default:
                verdictColor = NSColor(calibratedWhite: 0.85, alpha: 1.0)
            }
            widgets.tiles[5].valueLabel.textColor = verdictColor
            widgets.tiles[5].container.layer?.borderColor = verdictColor.withAlphaComponent(0.65).cgColor
        }
    }

    func createFullScreenWindows() {
        let screens = NSScreen.screens
        for screen in screens {
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

            let (contentView, widgets) = buildContentView(frame: screen.frame)
            window.contentView = contentView
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
            windows.append(window)
            screenWidgets.append(widgets)
        }
    }

    func buildContentView(frame: NSRect) -> (NSView, ScreenCardWidgets) {
        let rootView = NSView(frame: NSRect(origin: .zero, size: frame.size))
        rootView.wantsLayer = true

        let bgColor: NSColor
        let accentColor: NSColor
        let badgeText: String
        let headlineText: String
        let subText: String
        let iconSymbol: String

        if mode == "inspect" {
            bgColor = NSColor(calibratedRed: 0.03, green: 0.09, blue: 0.16, alpha: 0.94)
            accentColor = NSColor(calibratedRed: 0.24, green: 0.78, blue: 0.98, alpha: 1.0)
            iconSymbol = "🔋"
            badgeText = "[\(deviceName)] BATTERY HEALTH & FORENSICS HUD (0-RISK READ-ONLY IOKIT)"
            headlineText = "\(deviceName) · \(percent)%"
            subText = "Reading live AppleSmartBattery hardware telemetry..."
        } else if mode == "high" {
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
        let cardWidth: CGFloat = min(940, frame.width * 0.90)
        let cardHeight: CGFloat = min(620, max(540, frame.height - 60))
        let cardFrame = NSRect(
            x: (frame.width - cardWidth) / 2,
            y: (frame.height - cardHeight) / 2,
            width: cardWidth,
            height: cardHeight
        )

        let cardView = NSView(frame: cardFrame)
        cardView.wantsLayer = true
        cardView.layer?.backgroundColor = NSColor(calibratedWhite: 0.08, alpha: 0.92).cgColor
        cardView.layer?.cornerRadius = 28
        cardView.layer?.borderWidth = 3
        cardView.layer?.borderColor = accentColor.cgColor

        // Icon label
        let iconLabel = NSTextField(labelWithString: iconSymbol)
        iconLabel.font = NSFont.systemFont(ofSize: 48)
        iconLabel.alignment = .center
        iconLabel.frame = NSRect(x: 0, y: cardHeight - 68, width: cardWidth, height: 56)
        cardView.addSubview(iconLabel)

        // Badge label
        let badgeLabel = NSTextField(labelWithString: badgeText)
        badgeLabel.font = NSFont.boldSystemFont(ofSize: 16)
        badgeLabel.textColor = accentColor
        badgeLabel.alignment = .center
        badgeLabel.frame = NSRect(x: 24, y: cardHeight - 98, width: cardWidth - 48, height: 24)
        cardView.addSubview(badgeLabel)

        // Giant headline
        let titleLabel = NSTextField(labelWithString: headlineText)
        titleLabel.font = NSFont.systemFont(ofSize: 42, weight: .heavy)
        titleLabel.textColor = .white
        titleLabel.alignment = .center
        titleLabel.frame = NSRect(x: 24, y: cardHeight - 154, width: cardWidth - 48, height: 52)
        cardView.addSubview(titleLabel)

        // Subtitle description
        let descLabel = NSTextField(wrappingLabelWithString: subText)
        descLabel.font = NSFont.systemFont(ofSize: 16, weight: .medium)
        descLabel.textColor = NSColor(calibratedWhite: 0.88, alpha: 1.0)
        descLabel.alignment = .center
        descLabel.frame = NSRect(x: 42, y: cardHeight - 204, width: cardWidth - 84, height: 46)
        cardView.addSubview(descLabel)

        // 2x3 Live Hardware Telemetry Grid
        let tileTitles = [
            "TRUE HEALTH & CAPACITY",
            "CYCLES & GAS GAUGE",
            "LIVE POWER & RATE",
            "PACK TEMPERATURE",
            "CELL BALANCE & IMPEDANCE",
            "ANTI-SPOOFING FORENSICS",
        ]
        let marginX: CGFloat = 32
        let colGap: CGFloat = 12
        let rowGap: CGFloat = 12
        let tileW: CGFloat = (cardWidth - marginX * 2 - colGap * 2) / 3
        let tileH: CGFloat = 92
        let row1Y: CGFloat = cardHeight - 306
        let row2Y: CGFloat = row1Y - tileH - rowGap

        var tiles: [TelemetryTileViews] = []
        for idx in 0..<6 {
            let row = idx / 3
            let col = idx % 3
            let tx = marginX + CGFloat(col) * (tileW + colGap)
            let ty = (row == 0) ? row1Y : row2Y

            let tileBox = NSView(frame: NSRect(x: tx, y: ty, width: tileW, height: tileH))
            tileBox.wantsLayer = true
            tileBox.layer?.backgroundColor = NSColor(calibratedWhite: 0.13, alpha: 0.95).cgColor
            tileBox.layer?.cornerRadius = 14
            tileBox.layer?.borderWidth = 1.2
            tileBox.layer?.borderColor = NSColor(calibratedWhite: 0.28, alpha: 0.7).cgColor

            let tTitle = NSTextField(labelWithString: tileTitles[idx])
            tTitle.font = NSFont.boldSystemFont(ofSize: 11)
            tTitle.textColor = accentColor.withAlphaComponent(0.92)
            tTitle.frame = NSRect(x: 12, y: tileH - 24, width: tileW - 24, height: 16)
            tileBox.addSubview(tTitle)

            let tValue = NSTextField(labelWithString: "--")
            tValue.font = NSFont.systemFont(ofSize: 19, weight: .bold)
            tValue.textColor = .white
            tValue.lineBreakMode = .byTruncatingTail
            tValue.frame = NSRect(x: 12, y: tileH - 52, width: tileW - 24, height: 24)
            tileBox.addSubview(tValue)

            let tDetail = NSTextField(wrappingLabelWithString: "--")
            tDetail.font = NSFont.systemFont(ofSize: 11.5, weight: .regular)
            tDetail.textColor = NSColor(calibratedWhite: 0.76, alpha: 1.0)
            tDetail.maximumNumberOfLines = 2
            tDetail.frame = NSRect(x: 12, y: 6, width: tileW - 24, height: 30)
            tileBox.addSubview(tDetail)

            cardView.addSubview(tileBox)
            tiles.append(TelemetryTileViews(
                container: tileBox,
                titleLabel: tTitle,
                valueLabel: tValue,
                detailLabel: tDetail
            ))
        }

        // Primary dismiss button
        let btnWidth: CGFloat = 420
        let btnHeight: CGFloat = 48
        let dismissBtn = NSButton(frame: NSRect(
            x: (cardWidth - btnWidth) / 2,
            y: 82,
            width: btnWidth,
            height: btnHeight
        ))
        dismissBtn.title = "Dismiss (Esc / Enter / Space)"
        dismissBtn.bezelStyle = .regularSquare
        dismissBtn.isBordered = false
        dismissBtn.wantsLayer = true
        dismissBtn.layer?.backgroundColor = accentColor.cgColor
        dismissBtn.layer?.cornerRadius = 14
        dismissBtn.font = NSFont.boldSystemFont(ofSize: 18)
        dismissBtn.contentTintColor = NSColor.black
        dismissBtn.target = self
        dismissBtn.action = #selector(dismissAlert)
        dismissBtn.keyEquivalent = "\r"
        cardView.addSubview(dismissBtn)

        // Secondary button: Pause 2h
        let subBtnWidth: CGFloat = 380
        let subBtnHeight: CGFloat = 38
        let pause2hBtn = NSButton(frame: NSRect(
            x: (cardWidth - subBtnWidth) / 2,
            y: 28,
            width: subBtnWidth,
            height: subBtnHeight
        ))
        pause2hBtn.title = "Pause Fullscreen Alerts for 2 Hours (Meeting Mode)"
        pause2hBtn.bezelStyle = .regularSquare
        pause2hBtn.isBordered = false
        pause2hBtn.wantsLayer = true
        pause2hBtn.layer?.backgroundColor = NSColor(calibratedWhite: 0.22, alpha: 1.0).cgColor
        pause2hBtn.layer?.cornerRadius = 10
        pause2hBtn.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        pause2hBtn.contentTintColor = NSColor.white
        pause2hBtn.target = self
        pause2hBtn.action = #selector(pauseTwoHours)
        cardView.addSubview(pause2hBtn)

        rootView.addSubview(cardView)
        return (rootView, ScreenCardWidgets(headlineLabel: titleLabel, subLabel: descLabel, tiles: tiles))
    }

    @objc func dismissAlert() {
        telemetryTimer?.invalidate()
        powerWatcher?.stop()
        NSApp.terminate(nil)
    }

    @objc func pauseTwoHours() {
        telemetryTimer?.invalidate()
        powerWatcher?.stop()
        if !statePath.isEmpty {
            let resolvedURL = URL(fileURLWithPath: statePath).resolvingSymlinksInPath()
            var dict: [String: Any] = [:]
            if let data = try? Data(contentsOf: resolvedURL),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                dict = obj
            }
            dict["pause_until_ts"] = Date().timeIntervalSince1970 + 7200.0
            if let outData = try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted]) {
                try? outData.write(to: resolvedURL, options: [.atomic])
                try? FileManager.default.setAttributes(
                    [.posixPermissions: NSNumber(value: 0o600)],
                    ofItemAtPath: resolvedURL.path
                )
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
