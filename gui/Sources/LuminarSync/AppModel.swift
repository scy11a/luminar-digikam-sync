import AppKit
import Foundation
import Observation

struct AlertInfo {
    let title: String
    let message: String
}

@MainActor
@Observable
final class AppModel {
    static let luminarBundleID = "com.skylum.luminarneo"
    static let digikamBundleID = "org.digiKam"

    var overview: Overview?
    var tree: [FolderNode] = []
    var onlyDiffFolders = true
    var selectedFolder: String?

    var plan: PlanResult?
    var items: [PlanItem] = []
    var showAllItems = false
    var selection = Set<String>()
    var sortOrder = [KeyPathComparator(\PlanItem.name)]

    var check: CheckData?
    var checkedFolder: String?

    var busy: String?
    var message: String?
    var alert: AlertInfo?
    var lastBackup: String?

    var luminarRunning = false
    var digikamRunning = false

    private let runner = SyncRunner.locate()
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    init() {
        refreshRunning()
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.refreshRunning() }
            }
        }
    }

    var visibleItems: [PlanItem] {
        showAllItems ? items : items.filter { $0.action != .none }
    }

    var selectedStat: FolderStat? {
        overview?.folders.first { $0.path == selectedFolder }
    }

    var suggestedWrites: Int { plan?.summary.write ?? 0 }
    var suggestedPulls: Int { plan?.summary.pull ?? 0 }

    /// Writing sidecars requires quitting digiKam, writing the Luminar catalog requires quitting Luminar.
    var canApplySuggested: Bool {
        guard busy == nil else { return false }
        if suggestedWrites > 0 { return !digikamRunning }
        return suggestedPulls > 0 && !luminarRunning
    }
    var canPushSelected: Bool { busy == nil && !digikamRunning && !selection.isEmpty }
    var canPullSelected: Bool { busy == nil && !luminarRunning && !selection.isEmpty }

    func refreshRunning() {
        let ids = Set(NSWorkspace.shared.runningApplications.compactMap(\.bundleIdentifier))
        luminarRunning = ids.contains(Self.luminarBundleID)
        digikamRunning = ids.contains(Self.digikamBundleID)
    }

    // MARK: - loading

    func reloadAll() async {
        refreshRunning()
        await loadOverview()
        await loadFolder()
    }

    func loadOverview() async {
        guard let result = await call(Overview.self, ["--overview", "--json"], busy: "Reading the catalog…") else { return }
        overview = result
        rebuildTree()
    }

    func rebuildTree() {
        guard let overview else { return }
        tree = buildTree(roots: overview.roots, folders: overview.folders, onlyDiffs: onlyDiffFolders)
    }

    func loadFolder() async {
        guard let folder = selectedFolder else {
            plan = nil
            items = []
            return
        }
        guard let result = await call(PlanResult.self, ["--json", folder], busy: "Comparing the folder…") else { return }
        // another folder was selected in the meantime: drop this result
        guard folder == selectedFolder else { return }
        plan = result
        items = result.items.sorted(using: sortOrder)
        selection = selection.intersection(items.map(\.id))
    }

    func resort() {
        items.sort(using: sortOrder)
    }

    // MARK: - writing

    /// The folder's suggested writes (auto mode: the side that changed wins). Values changed in digiKam
    /// only go to Luminar while Luminar is not running.
    func applySuggested() async {
        guard let folder = selectedFolder else { return }
        refreshRunning()
        let pull = suggestedPulls > 0 && !luminarRunning
        await apply(["--json", "--apply", folder] + (pull ? ["--to-luminar"] : []),
                    sidecars: suggestedWrites > 0, luminar: pull)
    }

    /// On the selected images one side's values overwrite the other's.
    func applySelected(toLuminar: Bool) async {
        let listing = FileManager.default.temporaryDirectory.appendingPathComponent("luminarsync-\(UUID().uuidString).txt")
        do {
            try selection.sorted().joined(separator: "\n").write(to: listing, atomically: true, encoding: .utf8)
        } catch {
            alert = AlertInfo(title: "Error", message: "The selection could not be saved: \(error.localizedDescription)")
            return
        }
        defer { try? FileManager.default.removeItem(at: listing) }
        let prefer = toLuminar ? "digikam" : "luminar"
        await apply(["--json", "--apply", "--prefer", prefer, "--files-from", listing.path],
                    sidecars: !toLuminar, luminar: toLuminar)
    }

    private func apply(_ arguments: [String], sidecars: Bool, luminar: Bool) async {
        refreshRunning()
        if sidecars && digikamRunning {
            alert = AlertInfo(title: "digiKam is running", message: "Quit digiKam (Cmd+Q) before writing sidecars.")
            return
        }
        if luminar && luminarRunning {
            alert = AlertInfo(title: "Luminar is running", message: "Quit Luminar (Cmd+Q) before writing its catalog.")
            return
        }
        guard let result = await call(PlanResult.self, arguments, busy: "Writing…") else { return }
        if let backup = result.backup { lastBackup = backup }
        let written = result.summary.write - result.failed.count
        var lines: [String] = []
        if result.summary.write > 0 {
            lines.append("\(written) sidecars written (new: \(result.summary.newSidecars)).")
        }
        if result.pulled {
            lines.append("\(result.summary.pull) images written back to the Luminar catalog; visible the next time Luminar starts.")
        } else if result.summary.pull > 0 {
            lines.append("\(result.summary.pull) digiKam changes were not written to Luminar because Luminar is running.")
        }
        if lines.isEmpty { lines.append("Nothing to do.") }
        if !result.failed.isEmpty {
            lines.append("ERROR – \(result.failed.count) images failed:\n" + result.failed.prefix(10).joined(separator: "\n"))
        }
        if written > 0 {
            lines.append("In digiKam, select the album and run “Reread Metadata From Files”.")
        }
        message = lines.first
        alert = AlertInfo(title: result.failed.isEmpty ? "Done" : "Partly done", message: lines.joined(separator: "\n\n"))
        await reloadAll()
    }

    func undoLast() async {
        guard let backup = lastBackup else { return }
        guard let result = await call(UndoResult.self, ["--json", "--undo", backup], busy: "Undoing…") else { return }
        lastBackup = nil
        message = "Undone."
        var text = "\(result.undo.restored) sidecars restored, \(result.undo.deleted) new sidecars deleted."
        if result.undo.luminarRestored + result.undo.luminarSkipped > 0 {
            text += "\nLuminar: \(result.undo.luminarRestored) images restored"
            if result.undo.luminarSkipped > 0 {
                text += ", \(result.undo.luminarSkipped) skipped (changed in Luminar since)"
            }
            text += "."
        }
        text += "\n\nIn digiKam, re-read the affected album."
        alert = AlertInfo(title: "Undone", message: text)
        await reloadAll()
    }

    /// What would change or be lost in digiKam after “Reread Metadata From Files” (read-only).
    func checkDigikam(includeCloud: Bool = false) async {
        guard let folder = selectedFolder else { return }
        let args = ["--check-digikam", "--json", folder] + (includeCloud ? ["--include-cloud"] : [])
        let text = includeCloud ? "digiKam check (downloading cloud-only images)…" : "digiKam check…"
        guard let result = await call(CheckReport.self, args, busy: text) else { return }
        checkedFolder = folder
        check = result.check
    }

    func revealInFinder(_ ids: Set<String>) {
        NSWorkspace.shared.activateFileViewerSelecting(ids.map { URL(fileURLWithPath: $0) })
    }

    // MARK: - calling the command-line tool

    private func call<T: Decodable>(_ type: T.Type, _ arguments: [String], busy text: String) async -> T? {
        guard let runner else {
            alert = AlertInfo(title: "Error", message: "luminar_sync.py was not found in the app bundle.")
            return nil
        }
        busy = text
        defer { busy = nil }
        do {
            let output = try await runner.run(arguments)
            if let error = try? decoder.decode(ErrorResult.self, from: output.stdout) {
                alert = AlertInfo(title: "Error", message: error.error)
                return nil
            }
            do {
                return try decoder.decode(T.self, from: output.stdout)
            } catch {
                let detail = output.stderr.isEmpty ? error.localizedDescription : output.stderr
                alert = AlertInfo(title: "Unexpected response from the tool", message: detail)
                return nil
            }
        } catch {
            alert = AlertInfo(title: "The tool could not be started", message: error.localizedDescription)
            return nil
        }
    }
}
