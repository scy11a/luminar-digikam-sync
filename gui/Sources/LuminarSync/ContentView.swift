import SwiftUI

struct ContentView: View {
    @Bindable var model: AppModel
    @State private var confirmSuggested = false
    @State private var confirmSelected = false
    @State private var confirmPull = false
    @State private var confirmUndo = false

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 260, ideal: 320)
        } detail: {
            detail
        }
        .toolbar { toolbar }
        .safeAreaInset(edge: .bottom, spacing: 0) { statusBar }
        .onChange(of: model.selectedFolder) {
            Task { await model.loadFolder() }
        }
        .onChange(of: model.onlyDiffFolders) {
            model.rebuildTree()
        }
        .onChange(of: model.sortOrder) {
            model.resort()
        }
        .confirmationDialog(
            "Apply the suggested changes?",
            isPresented: $confirmSuggested
        ) {
            Button("Apply") { Task { await model.applySuggested() } }
        } message: {
            Text(suggestedText)
        }
        .confirmationDialog(
            "Overwrite with Luminar's values on the selected images?",
            isPresented: $confirmSelected
        ) {
            Button("\(model.selection.count) images: Luminar → digiKam") { Task { await model.applySelected(toLuminar: false) } }
        } message: {
            Text("Luminar's flag and rating are written to the selected images' sidecars, for conflicts and kept values as well (including removals).")
        }
        .confirmationDialog(
            "Write digiKam's values to Luminar for the selected images?",
            isPresented: $confirmPull
        ) {
            Button("\(model.selection.count) images: digiKam → Luminar") { Task { await model.applySelected(toLuminar: true) } }
        } message: {
            Text("digiKam's flag and rating are written to the Luminar catalog for the selected images, including removals. The whole catalog is backed up first, and Undo restores it.")
        }
        .confirmationDialog("Undo the last write?", isPresented: $confirmUndo) {
            Button("Undo", role: .destructive) { Task { await model.undoLast() } }
        } message: {
            Text(model.lastBackup ?? "")
        }
        .sheet(isPresented: Binding(get: { model.check != nil }, set: { if !$0 { model.check = nil } })) {
            if let check = model.check {
                CheckSheet(model: model, check: check)
            }
        }
        .alert(
            model.alert?.title ?? "",
            isPresented: Binding(get: { model.alert != nil }, set: { if !$0 { model.alert = nil } }),
            presenting: model.alert
        ) { _ in
            Button("OK") {}
        } message: { info in
            Text(info.message)
        }
    }

    private var suggestedText: String {
        var parts: [String] = []
        if model.suggestedWrites > 0 {
            parts.append("Write \(model.suggestedWrites) sidecars (Luminar → digiKam).")
        }
        if model.suggestedPulls > 0 {
            parts.append(model.luminarRunning
                ? "\(model.suggestedPulls) digiKam changes will not go to Luminar now, because Luminar is running."
                : "Write \(model.suggestedPulls) images back to the Luminar catalog (digiKam → Luminar).")
        }
        parts.append("Conflicts and kept values are left unchanged.")
        return parts.joined(separator: "\n")
    }

    // MARK: - sidebar

    private var sidebar: some View {
        List(model.tree, children: \.children, selection: $model.selectedFolder) { node in
            FolderRow(node: node)
        }
        .overlay {
            if model.tree.isEmpty && model.busy == nil {
                ContentUnavailableView(
                    model.onlyDiffFolders ? "No differences" : "No folders",
                    systemImage: "checkmark.circle",
                    description: Text(model.onlyDiffFolders ? "Luminar and digiKam agree in every folder." : "")
                )
            }
        }
        .safeAreaInset(edge: .bottom) {
            Toggle("Only folders with differences", isOn: $model.onlyDiffFolders)
                .toggleStyle(.checkbox)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: - details

    @ViewBuilder
    private var detail: some View {
        if let folder = model.selectedFolder {
            VStack(alignment: .leading, spacing: 0) {
                header(folder: folder)
                Divider()
                Table(model.visibleItems, selection: $model.selection, sortOrder: $model.sortOrder) {
                    TableColumn("File", value: \.name)
                        .width(min: 140, ideal: 200)
                    TableColumn("Luminar") { item in
                        ValuesView(values: item.luminar)
                    }
                    .width(min: 120, ideal: 140)
                    TableColumn("digiKam") { item in
                        ValuesView(values: item.digikam)
                    }
                    .width(min: 120, ideal: 140)
                    TableColumn("Action", value: \.actionRank) { item in
                        Text(item.action.label)
                            .foregroundStyle(item.action.color)
                            .fontWeight(item.action == .conflict ? .semibold : .regular)
                    }
                    .width(min: 70, ideal: 80)
                    TableColumn("Details") { item in
                        Text(item.details)
                            .foregroundStyle(.secondary)
                            .help(item.details)
                    }
                }
                .contextMenu(forSelectionType: String.self) { ids in
                    Button("Show in Finder") { model.revealInFinder(ids) }
                }
                .overlay {
                    if model.visibleItems.isEmpty && model.busy == nil && model.plan != nil {
                        ContentUnavailableView("No differences", systemImage: "checkmark.circle",
                                               description: Text("Every image in this folder agrees."))
                    }
                }
            }
        } else {
            ContentUnavailableView("Select a folder", systemImage: "folder",
                                   description: Text("The list on the left shows the Luminar folders; the number is the count of differences."))
        }
    }

    private func header(folder: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(folder)
                .font(.headline)
                .lineLimit(1)
                .truncationMode(.head)
                .help(folder)
            HStack(spacing: 14) {
                if let summary = model.plan?.summary {
                    Text("\(summary.selected) images")
                    Text("write: \(summary.write)").foregroundStyle(SyncAction.write.color)
                    if summary.pull > 0 {
                        Text("→ Luminar: \(summary.pull)").foregroundStyle(SyncAction.pull.color)
                    }
                    Text("conflict: \(summary.conflict)").foregroundStyle(SyncAction.conflict.color)
                    Text("kept: \(summary.keep)").foregroundStyle(SyncAction.keep.color)
                    Text("equal: \(summary.equal)").foregroundStyle(.secondary)
                    if summary.missing > 0 {
                        Text("missing files: \(summary.missing)").foregroundStyle(.orange)
                    }
                }
                Spacer()
                Picker("", selection: $model.showAllItems) {
                    Text("Differences").tag(false)
                    Text("All").tag(true)
                }
                .pickerStyle(.segmented)
                .fixedSize()
            }
            .font(.callout)
            if let reread = model.selectedStat?.reread, reread > 0 {
                Label("\(reread) images are already in sync, but digiKam has not re-read them yet: digiKam → select the album → Reread Metadata From Files.",
                      systemImage: "arrow.clockwise")
                    .font(.callout)
                    .foregroundStyle(.blue)
            }
        }
        .padding(12)
    }

    // MARK: - toolbar and status bar

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItemGroup {
            Button {
                Task { await model.reloadAll() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .help("Refresh (Cmd+R)")
            .disabled(model.busy != nil)

            Button {
                confirmSuggested = true
            } label: {
                Label("Apply suggested", systemImage: "wand.and.stars")
            }
            .help("The folder's suggested actions: “write” → digiKam sidecar, “→ Luminar” → Luminar catalog")
            .disabled(!model.canApplySuggested)

            Button {
                confirmSelected = true
            } label: {
                Label("Selected: Luminar → digiKam", systemImage: "arrow.right.circle")
            }
            .help("Writes Luminar's values to the digiKam sidecars of the selected images (conflicts included)")
            .disabled(!model.canPushSelected)

            Button {
                confirmPull = true
            } label: {
                Label("Selected: digiKam → Luminar", systemImage: "arrow.left.circle")
            }
            .help("Writes digiKam's values to the Luminar catalog for the selected images (only while Luminar is closed)")
            .disabled(!model.canPullSelected)

            Button {
                Task { await model.checkDigikam() }
            } label: {
                Label("digiKam check", systemImage: "checkmark.shield")
            }
            .help("What would change or be lost in digiKam after “Reread Metadata From Files” (read-only)")
            .disabled(model.selectedFolder == nil || model.busy != nil)

            Button {
                confirmUndo = true
            } label: {
                Label("Undo", systemImage: "arrow.uturn.backward")
            }
            .help("Undo the last write")
            .disabled(model.lastBackup == nil || model.busy != nil)
        }
    }

    private var statusBar: some View {
        HStack(spacing: 18) {
            AppStatus(name: "Luminar", running: model.luminarRunning,
                      note: model.luminarRunning ? "writing Luminar disabled" : nil)
            AppStatus(name: "digiKam", running: model.digikamRunning,
                      note: model.digikamRunning ? "writing sidecars disabled" : nil)
            Spacer()
            if let busy = model.busy {
                ProgressView().controlSize(.small)
                Text(busy)
            } else if let message = model.message {
                Text(message).foregroundStyle(.secondary)
            }
        }
        .font(.callout)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.bar)
    }
}

struct FolderRow: View {
    let node: FolderNode

    var body: some View {
        HStack {
            Label(node.name, systemImage: "folder")
                .lineLimit(1)
            Spacer()
            if node.differs > 0 {
                Badge(text: "\(node.differs)", color: .orange)
                    .help("\(node.differs) images whose flag or rating differs between Luminar and digiKam")
            }
            if node.reread > 0 {
                Badge(text: "↻ \(node.reread)", color: .blue)
                    .help("\(node.reread) images synced, waiting for a re-read in digiKam")
            }
        }
    }
}

struct Badge: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption.monospacedDigit())
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(color.opacity(0.2), in: Capsule())
            .foregroundStyle(color)
    }
}

struct ValuesView: View {
    let values: FieldValues

    var body: some View {
        HStack(spacing: 6) {
            Text(pickText(values.pick))
                .foregroundStyle(values.pick == 1 ? .red : values.pick == 3 ? .green : .secondary)
                .frame(width: 66, alignment: .leading)
            Text(ratingText(values.rating))
                .foregroundStyle(values.rating > 0 ? .primary : .secondary)
        }
    }
}

struct AppStatus: View {
    let name: String
    let running: Bool
    let note: String?

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(running ? Color.green : Color.gray.opacity(0.5))
                .frame(width: 8, height: 8)
            Text("\(name): \(running ? "running" : "not running")")
            if let note {
                Text("– \(note)").foregroundStyle(.orange)
            }
        }
    }
}

/// The result of --check-digikam: what would change or be lost in digiKam's database on a re-read.
struct CheckSheet: View {
    @Bindable var model: AppModel
    let check: CheckData
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("digiKam check").font(.title2.bold())
            Text(model.checkedFolder ?? "")
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.head)
            HStack(spacing: 16) {
                Text("\(check.summary.images) images")
                Text("would be overwritten: \(check.summary.overwritten)").foregroundStyle(.red)
                Text("only in the database: \(check.summary.dbOnly)").foregroundStyle(.orange)
                if check.summary.unchecked > 0 {
                    Text("not checked (cloud only): \(check.summary.unchecked)").foregroundStyle(.secondary)
                }
            }
            .font(.callout)

            let problems = check.items.filter { !$0.unchecked }
            if problems.isEmpty {
                Label("A re-read would not overwrite or lose anything\(check.summary.unchecked > 0 ? " on the locally available images" : "").",
                      systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
                Spacer()
            } else {
                List(problems) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.name).fontWeight(.semibold)
                        ForEach(Array(item.issues.enumerated()), id: \.offset) { _, issue in
                            Text(issue.text)
                                .foregroundStyle(issue.kind == "overwritten" ? .red : .orange)
                                .font(.callout)
                        }
                    }
                }
                Text("Suggestion: before re-reading, select the album in digiKam and write the database values to the files (Album → Write Metadata to Files). Do this BEFORE writing with LuminarSync.")
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                if check.summary.unchecked > 0 {
                    Button("Also check cloud-only images (downloads them)") {
                        Task { await model.checkDigikam(includeCloud: true) }
                    }
                    .disabled(model.busy != nil)
                }
                Spacer()
                if let busy = model.busy {
                    ProgressView().controlSize(.small)
                    Text(busy).font(.callout)
                }
                Button("Close") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(minWidth: 640, minHeight: 420)
    }
}
