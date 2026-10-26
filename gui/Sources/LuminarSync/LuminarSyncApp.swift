import SwiftUI

@main
struct LuminarSyncApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        Window("LuminarSync", id: "main") {
            ContentView(model: model)
                .frame(minWidth: 1000, minHeight: 600)
                .task { await model.reloadAll() }
        }
        .commands {
            CommandGroup(after: .toolbar) {
                Button("Refresh") { Task { await model.reloadAll() } }
                    .keyboardShortcut("r")
            }
        }
    }
}
