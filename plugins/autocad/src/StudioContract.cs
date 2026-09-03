using System;
using System.IO;

namespace TopoSpatial.AutoCAD
{
    public sealed class StudioMessage
    {
        public int Version { get; set; }
        public string Action { get; set; }
        public string ExpectedDrawing { get; set; }
        public string SemanticId { get; set; }
        public string Handle { get; set; }
    }

    public sealed class StudioAcknowledgment
    {
        public int Version { get; set; } = 1;
        public string RequestAction { get; set; }
        public bool Success { get; set; }
        public string Code { get; set; }
        public string Message { get; set; }
        public string ActiveDrawing { get; set; }
        public string ExpectedDrawing { get; set; }
    }

    public static class StudioContract
    {
        public static bool IsSupportedAction(string action) =>
            action == "focus_entity" || action == "select_entity" || action == "request_status";

        public static bool IsTrustedSource(string source)
        {
            if (!Uri.TryCreate(source, UriKind.Absolute, out var uri)) return false;
            return string.Equals(uri.Scheme, "http", StringComparison.OrdinalIgnoreCase)
                && string.Equals(uri.Host, "127.0.0.1", StringComparison.OrdinalIgnoreCase)
                && uri.Port == 8888;
        }

        public static bool DrawingNamesMatch(string expected, string active)
        {
            if (string.IsNullOrWhiteSpace(expected) || string.IsNullOrWhiteSpace(active)) return false;
            string Normalize(string value)
            {
                var expanded = Environment.ExpandEnvironmentVariables(value.Trim()).Replace('/', Path.DirectorySeparatorChar);
                try { return Path.GetFullPath(expanded).TrimEnd(Path.DirectorySeparatorChar).ToUpperInvariant(); }
                catch { return expanded.TrimEnd(Path.DirectorySeparatorChar).ToUpperInvariant(); }
            }
            var expectedNormalized = Normalize(expected);
            var activeNormalized = Normalize(active);
            if (expected.IndexOfAny(new[] { '\\', '/' }) >= 0)
                return string.Equals(expectedNormalized, activeNormalized, StringComparison.OrdinalIgnoreCase);
            return string.Equals(Path.GetFileName(expectedNormalized), Path.GetFileName(activeNormalized), StringComparison.OrdinalIgnoreCase);
        }
    }
}
