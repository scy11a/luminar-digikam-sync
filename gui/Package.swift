// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "LuminarSync",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "LuminarSync", path: "Sources/LuminarSync"),
    ]
)
