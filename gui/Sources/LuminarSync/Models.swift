import Foundation
import SwiftUI

// Types matching the luminar_sync.py --json output (decoded with snake_case → camelCase).

struct Overview: Decodable {
    let catalog: String
    let roots: [String]
    let folders: [FolderStat]
}

struct FolderStat: Decodable {
    let path: String
    let images: Int
    let flagged: Int
    let rated: Int
    let differs: Int
    let reread: Int
}

struct PlanResult: Decodable {
    let applied: Bool
    let pulled: Bool
    let backup: String?
    let failed: [String]
    let summary: Summary
    let missing: [String]
    let notInCatalog: [String]
    let items: [PlanItem]
}

struct Summary: Decodable {
    let selected: Int
    let write: Int
    let newSidecars: Int
    let pull: Int
    let conflict: Int
    let keep: Int
    let equal: Int
    let missing: Int
    let notInCatalog: Int
}

struct FieldValues: Decodable {
    let pick: Int
    let rating: Int
}

struct Decision: Decodable {
    let action: String
    let reason: String
}

struct UndoResult: Decodable {
    struct Counts: Decodable {
        let restored: Int
        let deleted: Int
        let luminarRestored: Int
        let luminarSkipped: Int
    }

    let undo: Counts
}

struct ErrorResult: Decodable {
    let error: String
}

enum SyncAction: Int, Comparable {
    case none, keep, pull, write, conflict

    init(_ raw: String) {
        switch raw {
        case "write": self = .write
        case "pull": self = .pull
        case "keep": self = .keep
        case "conflict": self = .conflict
        default: self = .none
        }
    }

    static func < (lhs: SyncAction, rhs: SyncAction) -> Bool { lhs.rawValue < rhs.rawValue }

    var label: String {
        switch self {
        case .none: "equal"
        case .keep: "kept"
        case .pull: "→ Luminar"
        case .write: "write"
        case .conflict: "conflict"
        }
    }

    var color: Color {
        switch self {
        case .none: .secondary
        case .keep: .blue
        case .pull: .purple
        case .write: .green
        case .conflict: .red
        }
    }
}

struct PlanItem: Decodable, Identifiable {
    let image: String
    let hasSidecar: Bool
    let luminar: FieldValues
    let digikam: FieldValues
    let decisions: [String: Decision]

    var id: String { image }
    var name: String { (image as NSString).lastPathComponent }

    /// The “strongest” action of the two fields (conflict > write > → Luminar > kept > equal).
    var action: SyncAction { decisions.values.map { SyncAction($0.action) }.max() ?? .none }
    var actionRank: Int { action.rawValue }

    var details: String {
        var parts: [String] = []
        for (key, label) in [("pick", "flag"), ("rating", "rating")] {
            guard let d = decisions[key], SyncAction(d.action) != .none else { continue }
            let lum = key == "pick" ? pickText(luminar.pick) : ratingText(luminar.rating)
            let dk = key == "pick" ? pickText(digikam.pick) : ratingText(digikam.rating)
            let change: String
            switch SyncAction(d.action) {
            case .write: change = "\(dk) → \(lum)"
            case .pull: change = "Luminar \(lum) → \(dk)"
            default: change = "digiKam \(dk), Luminar \(lum)"
            }
            parts.append("\(label): \(change) (\(d.reason))")
        }
        if action == .write && !hasSidecar { parts.append("new sidecar") }
        return parts.joined(separator: "; ")
    }
}

func pickText(_ value: Int) -> String {
    switch value {
    case 1: "Rejected"
    case 2: "Pending"
    case 3: "Accepted"
    default: "–"
    }
}

func ratingText(_ value: Int) -> String {
    value > 0 ? String(repeating: "★", count: value) : "–"
}

/// A node of the folder tree; the counts include all subfolders.
final class FolderNode: Identifiable {
    let id: String
    let name: String
    var children: [FolderNode]?
    var images = 0
    var differs = 0
    var reread = 0

    init(id: String, name: String) {
        self.id = id
        self.name = name
    }
}

func buildTree(roots: [String], folders: [FolderStat], onlyDiffs: Bool) -> [FolderNode] {
    var nodes: [String: FolderNode] = [:]
    var rootNodes: [FolderNode] = []
    for root in roots {
        let parts = root.split(separator: "/").suffix(2).joined(separator: "/")
        let node = FolderNode(id: root, name: parts)
        nodes[root] = node
        rootNodes.append(node)
    }

    func node(for path: String) -> FolderNode? {
        if let existing = nodes[path] { return existing }
        let parentPath = (path as NSString).deletingLastPathComponent
        guard parentPath != path, parentPath.count > 1, let parent = node(for: parentPath) else { return nil }
        let created = FolderNode(id: path, name: (path as NSString).lastPathComponent)
        nodes[path] = created
        parent.children = (parent.children ?? []) + [created]
        return created
    }

    for stat in folders where !onlyDiffs || stat.differs + stat.reread > 0 {
        guard node(for: stat.path) != nil else { continue }  // not under any Luminar folder
        var path = stat.path
        while let current = nodes[path] {
            current.images += stat.images
            current.differs += stat.differs
            current.reread += stat.reread
            let parentPath = (path as NSString).deletingLastPathComponent
            if parentPath == path { break }
            path = parentPath
        }
    }

    func sortChildren(_ node: FolderNode) {
        node.children?.sort { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
        node.children?.forEach(sortChildren)
    }
    rootNodes.forEach(sortChildren)
    return onlyDiffs ? rootNodes.filter { $0.children != nil } : rootNodes
}

// --check-digikam --json
struct CheckReport: Decodable {
    let check: CheckData
}

struct CheckData: Decodable {
    let summary: CheckSummary
    let items: [CheckItem]
}

struct CheckSummary: Decodable {
    let images: Int
    let overwritten: Int
    let dbOnly: Int
    let unchecked: Int
}

struct CheckIssue: Decodable {
    let field: String
    let kind: String
    let db: String
    let file: String?

    var text: String {
        let label = ["pick": "flag", "color": "color label", "rating": "rating", "tag": "tag", "caption": "caption"][field] ?? field
        if kind == "overwritten" {
            return "\(label): database “\(db)”, file “\(file ?? "")” – would be overwritten"
        }
        return "\(label): “\(db)” – only in the database"
    }
}

struct CheckItem: Decodable, Identifiable {
    let image: String
    let unchecked: Bool
    let issues: [CheckIssue]

    var id: String { image }
    var name: String { (image as NSString).lastPathComponent }
}
