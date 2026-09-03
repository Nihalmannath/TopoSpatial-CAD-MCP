using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using Autodesk.AutoCAD.Windows;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using CadApplication = Autodesk.AutoCAD.ApplicationServices.Application;

namespace TopoSpatial.AutoCAD
{
    public sealed class TopoSpatialPlugin : IExtensionApplication
    {
        private const string PluginVersion = "0.3.0";
        private static readonly Uri Service = new Uri("http://127.0.0.1:8888/");
        private static readonly HttpClient Http = new HttpClient
        {
            BaseAddress = Service,
            Timeout = TimeSpan.FromSeconds(5)
        };
        private static readonly object Sync = new object();
        private static readonly HashSet<string> Handles = new HashSet<string>();
        private static PaletteSet _palette;
        private static WebView2 _browser;
        private static Panel _statusPanel;
        private static Label _statusLabel;
        private static long _sequence;
        private static int _sending;
        private static string _eventType = "document.changed";
        private static string _commandName = "";
        private static bool _eventsEnabled = false;
        private static bool _globalEventsAttached = false;

        public void Initialize()
        {
            // Events are disabled by default to prevent COM thread contention
            // with the MCP server. Use TOPOSTUDIOEVENTS to opt-in.
            if (_eventsEnabled)
                AttachGlobalEvents();
        }

        public void Terminate()
        {
            DetachGlobalEvents();
            _palette?.Dispose();
        }

        private static void AttachGlobalEvents()
        {
            if (!_eventsEnabled || _globalEventsAttached) return;
            CadApplication.DocumentManager.DocumentCreated += OnDocumentCreated;
            CadApplication.DocumentManager.DocumentActivated += OnDocumentActivated;
            CadApplication.DocumentManager.DocumentToBeDestroyed += OnDocumentDestroyed;
            CadApplication.Idle += OnIdle;
            foreach (Document document in CadApplication.DocumentManager)
                Attach(document);
            _globalEventsAttached = true;
        }

        private static void DetachGlobalEvents()
        {
            if (!_globalEventsAttached) return;
            CadApplication.DocumentManager.DocumentCreated -= OnDocumentCreated;
            CadApplication.DocumentManager.DocumentActivated -= OnDocumentActivated;
            CadApplication.DocumentManager.DocumentToBeDestroyed -= OnDocumentDestroyed;
            CadApplication.Idle -= OnIdle;
            foreach (Document document in CadApplication.DocumentManager)
                Detach(document);
            _globalEventsAttached = false;
        }

        [CommandMethod("TOPOSTUDIO", CommandFlags.Session)]
        public static void ShowEditor()
        {
            if (_palette == null)
            {
                var host = new UserControl { Dock = DockStyle.Fill };
                _browser = new WebView2
                {
                    Dock = DockStyle.Fill,
                    Visible = false,
                    CreationProperties = new CoreWebView2CreationProperties
                    {
                        UserDataFolder = Path.Combine(
                            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                            "TopoSpatial-CAD-MCP",
                            "WebView2")
                    }
                };
                _browser.NavigationCompleted += OnNavigationCompleted;

                _statusLabel = new Label
                {
                    AutoSize = false,
                    Dock = DockStyle.Top,
                    Height = 130,
                    Padding = new Padding(24),
                    TextAlign = ContentAlignment.MiddleCenter
                };
                var retry = new Button
                {
                    AutoSize = true,
                    Text = "Retry editor connection"
                };
                retry.Click += (sender, args) => NavigateEditor();
                _statusPanel = new Panel
                {
                    BackColor = Color.White,
                    Dock = DockStyle.Fill
                };
                _statusPanel.Controls.Add(retry);
                _statusPanel.Controls.Add(_statusLabel);
                retry.Left = 24;
                retry.Top = 145;

                host.Controls.Add(_browser);
                host.Controls.Add(_statusPanel);
                _palette = new PaletteSet("TopoSpatial Studio")
                {
                    Size = new System.Drawing.Size(1180, 780),
                    DockEnabled = DockSides.Left | DockSides.Right
                };
                _palette.Add("Architectural Space Program & Graph", host);
            }
            _palette.Visible = true;
            NavigateEditor();
        }

        [CommandMethod("TOPOSTUDIORELOAD", CommandFlags.Session)]
        public static void ReloadEditor()
        {
            ShowEditor();
        }

        /// <summary>
        /// Toggle live CAD event forwarding to the TopoSpatial visual studio.
        /// </summary>
        [CommandMethod("TOPOSTUDIOEVENTS", CommandFlags.Session)]
        public static void ToggleEvents()
        {
            var editor = CadApplication.DocumentManager.MdiActiveDocument?.Editor;
            _eventsEnabled = !_eventsEnabled;
            if (_eventsEnabled)
            {
                AttachGlobalEvents();
                editor?.WriteMessage("\n[TopoSpatial] Live CAD event stream ENABLED. Drawing changes will sync to Studio in real time.\n");
            }
            else
            {
                DetachGlobalEvents();
                editor?.WriteMessage("\n[TopoSpatial] Live CAD event stream DISABLED. Use manual Refresh in Studio.\n");
            }
        }

        /// <summary>
        /// Print connection and synchronization status to the AutoCAD command prompt.
        /// </summary>
        [CommandMethod("TOPOSTATUS", CommandFlags.Session)]
        public static async void ShowStatus()
        {
            var editor = CadApplication.DocumentManager.MdiActiveDocument?.Editor;
            if (editor == null) return;

            editor.WriteMessage("\n--- TopoSpatial CAD Plugin Status ---");
            editor.WriteMessage($"\nPlugin Version   : {PluginVersion}");
            editor.WriteMessage($"\nAutoCAD Product  : {Convert.ToString(CadApplication.GetSystemVariable("PLATFORM"), CultureInfo.InvariantCulture)}");
            editor.WriteMessage($"\nAutoCAD Version  : {Convert.ToString(CadApplication.GetSystemVariable("ACADVER"), CultureInfo.InvariantCulture)}");
            editor.WriteMessage($"\nActive Drawing   : {CadApplication.DocumentManager.MdiActiveDocument?.Name ?? "(none)"}");
            editor.WriteMessage($"\nService Endpoint : {Service}");
            editor.WriteMessage($"\nLive Sync Stream : {(_eventsEnabled ? "ENABLED (Active)" : "DISABLED (Paused)")}");

            try
            {
                using (var response = await Http.GetAsync("api/health"))
                {
                    if (response.IsSuccessStatusCode)
                    {
                        var health = JObject.Parse(await response.Content.ReadAsStringAsync());
                        editor.WriteMessage("\nMCP Server State : CONNECTED");
                        editor.WriteMessage($"\nBackend Version  : {health.Value<string>("version") ?? "unknown"}");
                        editor.WriteMessage($"\nFrontend Version : {PluginVersion}");
                    }
                    else
                    {
                        editor.WriteMessage($"\nMCP Server State : ERROR (HTTP {(int)response.StatusCode})");
                    }
                }
            }
            catch (System.Exception ex)
            {
                editor.WriteMessage($"\nMCP Server State : OFFLINE ({ex.Message})");
            }
            try
            {
                var drawing = CadApplication.DocumentManager.MdiActiveDocument?.Name;
                if (!string.IsNullOrWhiteSpace(drawing))
                {
                    using (var workspaceResponse = await Http.GetAsync("api/editor/workspace?include_candidates=false&expected_drawing=" + Uri.EscapeDataString(drawing)))
                    {
                        if (workspaceResponse.IsSuccessStatusCode)
                        {
                            var payload = JObject.Parse(await workspaceResponse.Content.ReadAsStringAsync());
                            var workspace = payload["workspace"] as JObject;
                            editor.WriteMessage($"\nDrawing Revision : {Short(payload.Value<string>("drawing_revision"))}");
                            editor.WriteMessage($"\nDraft State      : {DraftState(workspace)}");
                            editor.WriteMessage($"\nPending Requests : {workspace?.Value<int?>("pending_editor_request_count") ?? 0}");
                        }
                    }
                }
            }
            catch (System.Exception ex)
            {
                editor.WriteMessage($"\nWorkspace State  : unavailable ({ex.Message})");
            }
            editor.WriteMessage("\n------------------------------------\n");
        }

        /// <summary>
        /// Zoom to an AutoCAD entity by its hexadecimal handle.
        /// </summary>
        [CommandMethod("TOPOZOOM", CommandFlags.Modal)]
        public static void ZoomToHandleCommand()
        {
            var doc = CadApplication.DocumentManager.MdiActiveDocument;
            if (doc == null) return;

            var pso = new PromptStringOptions("\nEnter entity handle (hex, e.g. 1A4F): ")
            {
                AllowSpaces = false
            };
            var res = doc.Editor.GetString(pso);
            if (res.Status != PromptStatus.OK || string.IsNullOrWhiteSpace(res.StringResult)) return;

            ZoomToHandle(doc, res.StringResult.Trim());
        }

        /// <summary>
        /// Display Space Programming and Area Schedule overview in the AutoCAD command line.
        /// </summary>
        [CommandMethod("TOPOPROGRAM", CommandFlags.Session)]
        public static async void ShowSpaceProgram()
        {
            var editor = CadApplication.DocumentManager.MdiActiveDocument?.Editor;
            if (editor == null) return;

            try
            {
                var docName = CadApplication.DocumentManager.MdiActiveDocument?.Name ?? "";
                var url = string.IsNullOrWhiteSpace(docName)
                    ? "api/editor/workspace"
                    : "api/editor/workspace?expected_drawing=" + Uri.EscapeDataString(docName);

                using (var res = await Http.GetAsync(url))
                {
                    if (!res.IsSuccessStatusCode)
                    {
                        editor.WriteMessage("\n[TopoSpatial] Could not retrieve workspace topology from server.\n");
                        return;
                    }

                    var json = await res.Content.ReadAsStringAsync();
                    var payload = JObject.Parse(json);
                    var graph = payload["graph"] as JObject;
                    var elements = graph?["@graph"] as JArray ?? new JArray();

                    editor.WriteMessage("\n================ TOPOSPATIAL SPACE PROGRAM SCHEDULE ================");
                    editor.WriteMessage($"\nDrawing: {Path.GetFileName(docName)}  |  Revision: {Short(payload.Value<string>("drawing_revision"))}\n");
                    editor.WriteMessage(string.Format(CultureInfo.InvariantCulture, "\n{0,-20} {1,-14} {2,-14} {3,12}", "Space Label", "Type", "Zone", "Area (m²)"));
                    editor.WriteMessage("\n------------------------------------------------------------------");

                    double totalArea = 0.0;
                    int spaceCount = 0;
                    int pathCount = 0;

                    foreach (var elem in elements)
                    {
                        var type = elem.Value<string>("@type") ?? "";
                        if (type == "top:Space" || type == "top:Room")
                        {
                            spaceCount++;
                            var label = elem.Value<string>("rdfs:label") ?? elem.Value<string>("@id")?.Split(':').LastOrDefault() ?? "Space";
                            var props = elem["cad:properties"] as JObject;
                            var spaceType = props?.Value<string>("space_type") ?? "other";
                            var zone = props?.Value<string>("zone") ?? "unassigned";
                            var area = elem.Value<double?>("cad:areaSquareMetres") ?? ((elem.Value<double?>("top:hasArea") ?? 0.0) / 1000000.0);
                            totalArea += area;

                            editor.WriteMessage(string.Format(CultureInfo.InvariantCulture, "\n{0,-20} {1,-14} {2,-14} {3,12:F2}",
                                label.Length > 20 ? label.Substring(0, 17) + "..." : label,
                                spaceType.Length > 14 ? spaceType.Substring(0, 11) + "..." : spaceType,
                                zone.Length > 14 ? zone.Substring(0, 11) + "..." : zone,
                                area));
                        }
                        else if (type == "top:Connection")
                        {
                            pathCount++;
                        }
                    }

                    editor.WriteMessage("\n------------------------------------------------------------------");
                    editor.WriteMessage(string.Format(CultureInfo.InvariantCulture, "\nTOTAL: {0} Spaces  |  {1:F2} m²  |  {2} Circulation Connections", spaceCount, totalArea, pathCount));
                    editor.WriteMessage("\nUse 'TOPOSTUDIO' to open the interactive Visual Schedule & 2D Editor.");
                    editor.WriteMessage("\n==================================================================\n");
                }
            }
            catch (System.Exception ex)
            {
                editor.WriteMessage($"\n[TopoSpatial] Error fetching space program: {ex.Message}\n");
            }
        }

        private static async void NavigateEditor()
        {
            if (_browser == null) return;
            ShowStatus("Connecting to TopoSpatial Studio at " + Service + " ...");
            try
            {
                using (var response = await Http.GetAsync("editor"))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        ShowStatus(
                            "The TopoSpatial server returned HTTP " + (int)response.StatusCode +
                            " for /editor. Ensure the MCP server is running, then click Retry.");
                        return;
                    }
                }

                await _browser.EnsureCoreWebView2Async();

                // Hook up bidirectional message bridge
                _browser.CoreWebView2.WebMessageReceived -= OnWebMessageReceived;
                _browser.CoreWebView2.WebMessageReceived += OnWebMessageReceived;

                _browser.CoreWebView2.Navigate(
                    new Uri(Service, "editor?desktop=" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()).AbsoluteUri);
            }
            catch (System.Exception exception)
            {
                ShowStatus(
                    "TopoSpatial Studio could not start. Confirm the MCP/dashboard server is running " +
                    "on 127.0.0.1:8888 and that Microsoft Edge WebView2 Runtime is installed.\n\n" +
                    exception.Message);
            }
        }

        private static void OnWebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            StudioMessage request = null;
            try
            {
                if (!StudioContract.IsTrustedSource(e.Source))
                {
                    SendAcknowledgment(null, false, "INVALID_SOURCE", "Messages are accepted only from the local TopoSpatial editor.", null);
                    return;
                }
                request = JsonConvert.DeserializeObject<StudioMessage>(e.WebMessageAsJson);
                if (request == null || request.Version != 1)
                {
                    SendAcknowledgment(request, false, "UNSUPPORTED_VERSION", "Studio message version 1 is required.", null);
                    return;
                }
                if (!StudioContract.IsSupportedAction(request.Action))
                {
                    SendAcknowledgment(request, false, "UNSUPPORTED_ACTION", "The requested Studio action is not supported.", null);
                    return;
                }
                var doc = CadApplication.DocumentManager.MdiActiveDocument;
                if (doc == null)
                {
                    SendAcknowledgment(request, false, "NO_ACTIVE_DRAWING", "No AutoCAD drawing is active.", null);
                    return;
                }
                if (string.IsNullOrWhiteSpace(request.ExpectedDrawing))
                {
                    SendAcknowledgment(request, false, "EXPECTED_DRAWING_REQUIRED", "The message must name its pinned drawing.", doc.Name);
                    return;
                }
                if (!StudioContract.DrawingNamesMatch(request.ExpectedDrawing, doc.Name))
                {
                    SendAcknowledgment(request, false, "DRAWING_MISMATCH", "The active drawing is not the drawing pinned by the Studio.", doc.Name);
                    return;
                }
                if (request.Action == "request_status")
                {
                    SendAcknowledgment(request, true, "OK", "The pinned drawing is active.", doc.Name);
                    return;
                }
                if (string.IsNullOrWhiteSpace(request.Handle))
                {
                    SendAcknowledgment(request, false, "HANDLE_REQUIRED", "The selected semantic entity has no CAD handle.", doc.Name);
                    return;
                }
                var focused = FocusOrSelect(doc, request.Handle, request.Action == "focus_entity", out var failure);
                SendAcknowledgment(request, focused, focused ? "OK" : "ENTITY_NOT_FOUND", focused ? "CAD entity selected in the pinned drawing." : failure, doc.Name);
            }
            catch (System.Exception ex)
            {
                SendAcknowledgment(request, false, "INVALID_MESSAGE", ex.Message, CadApplication.DocumentManager.MdiActiveDocument?.Name);
            }
        }

        private static bool FocusOrSelect(Document doc, string handleHex, bool focus, out string failure)
        {
            failure = null;
            if (!long.TryParse(handleHex.Trim(), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var value))
            {
                failure = "The CAD handle is not valid hexadecimal.";
                return false;
            }
            try
            {
                using (doc.LockDocument())
                using (var tr = doc.TransactionManager.StartTransaction())
                {
                    if (!doc.Database.TryGetObjectId(new Handle(value), out ObjectId objectId))
                    {
                        failure = "The CAD handle does not exist in the pinned drawing.";
                        return false;
                    }
                    var entity = tr.GetObject(objectId, OpenMode.ForRead) as Entity;
                    if (entity == null) { failure = "The handle is not a selectable CAD entity."; return false; }
                    doc.Editor.SetImpliedSelection(new[] { objectId });
                    if (focus) ZoomWindow(doc.Editor, entity.GeometricExtents.MinPoint, entity.GeometricExtents.MaxPoint);
                    tr.Commit();
                    return true;
                }
            }
            catch (System.Exception ex) { failure = ex.Message; return false; }
        }

        private static void SendAcknowledgment(StudioMessage request, bool success, string code, string message, string activeDrawing)
        {
            if (_browser?.CoreWebView2 == null) return;
            var response = new StudioAcknowledgment
            {
                RequestAction = request?.Action,
                Success = success,
                Code = code,
                Message = message,
                ActiveDrawing = activeDrawing,
                ExpectedDrawing = request?.ExpectedDrawing
            };
            _browser.CoreWebView2.PostWebMessageAsJson(JsonConvert.SerializeObject(response, new JsonSerializerSettings { ContractResolver = new Newtonsoft.Json.Serialization.CamelCasePropertyNamesContractResolver() }));
        }

        private static string Short(string value) => string.IsNullOrWhiteSpace(value) ? "unknown" : value.Substring(0, Math.Min(12, value.Length));
        private static string DraftState(JObject workspace)
        {
            if (workspace == null) return "unknown";
            if (workspace.Value<bool?>("draft_frozen") == true) return "frozen";
            if ((workspace.Value<int?>("conflict_count") ?? 0) > 0) return "conflicted";
            return workspace.Value<bool?>("draft_dirty") == true ? "dirty" : "clean";
        }

        private static void ZoomToHandle(Document doc, string handleHex)
        {
            try
            {
                long val = long.Parse(handleHex, NumberStyles.HexNumber);
                var handle = new Handle(val);

                using (doc.LockDocument())
                using (var tr = doc.TransactionManager.StartTransaction())
                {
                    if (!doc.Database.TryGetObjectId(handle, out ObjectId objId))
                    {
                        doc.Editor.WriteMessage($"\n[TopoSpatial] Handle '{handleHex}' not found in drawing.\n");
                        return;
                    }

                    var ent = tr.GetObject(objId, OpenMode.ForRead) as Entity;
                    if (ent != null)
                    {
                        var ext = ent.GeometricExtents;
                        ZoomWindow(doc.Editor, ext.MinPoint, ext.MaxPoint);
                        doc.Editor.SetImpliedSelection(new[] { objId });
                        doc.Editor.WriteMessage($"\n[TopoSpatial] Focused on entity {handleHex} ({ent.GetType().Name}).\n");
                    }
                    tr.Commit();
                }
            }
            catch (System.Exception ex)
            {
                doc.Editor.WriteMessage($"\n[TopoSpatial] Zoom error: {ex.Message}\n");
            }
        }

        private static void ZoomWindow(Editor ed, Point3d min, Point3d max)
        {
            using (var view = ed.GetCurrentView())
            {
                var center = new Point2d((min.X + max.X) / 2.0, (min.Y + max.Y) / 2.0);
                double width = Math.Max(Math.Abs(max.X - min.X) * 1.3, 1000.0);
                double height = Math.Max(Math.Abs(max.Y - min.Y) * 1.3, 1000.0);
                view.CenterPoint = center;
                view.Width = width;
                view.Height = height;
                ed.SetCurrentView(view);
            }
        }

        private static void OnNavigationCompleted(
            object sender,
            Microsoft.Web.WebView2.Core.CoreWebView2NavigationCompletedEventArgs args)
        {
            if (args.IsSuccess)
            {
                _statusPanel.Visible = false;
                _browser.Visible = true;
                return;
            }

            ShowStatus(
                "The TopoSpatial Studio page failed to load (" + args.WebErrorStatus +
                "). Check the local server and click Retry.");
        }

        private static void ShowStatus(string message)
        {
            if (_statusLabel != null) _statusLabel.Text = message;
            if (_statusPanel != null)
            {
                _statusPanel.Visible = true;
                _statusPanel.BringToFront();
            }
            if (_browser != null) _browser.Visible = false;
        }

        private static void OnDocumentCreated(object sender, DocumentCollectionEventArgs e) => Attach(e.Document);
        private static void OnDocumentDestroyed(object sender, DocumentCollectionEventArgs e) => Detach(e.Document);
        private static void OnDocumentActivated(object sender, DocumentCollectionEventArgs e) => Queue("document.activated", "", null);

        private static void Attach(Document document)
        {
            if (!_eventsEnabled) return;
            document.CommandEnded += OnCommandEnded;
            document.CommandCancelled += OnCommandCancelled;
            document.CommandFailed += OnCommandFailed;
            document.Database.ObjectAppended += OnObjectChanged;
            document.Database.ObjectModified += OnObjectChanged;
            document.Database.ObjectErased += OnObjectErased;
        }

        private static void Detach(Document document)
        {
            document.CommandEnded -= OnCommandEnded;
            document.CommandCancelled -= OnCommandCancelled;
            document.CommandFailed -= OnCommandFailed;
            document.Database.ObjectAppended -= OnObjectChanged;
            document.Database.ObjectModified -= OnObjectChanged;
            document.Database.ObjectErased -= OnObjectErased;
        }

        private static void OnObjectChanged(object sender, ObjectEventArgs e) => Queue("object.changed", "", e.DBObject);
        private static void OnObjectErased(object sender, ObjectErasedEventArgs e) => Queue("object.erased", "", e.DBObject);
        private static void OnCommandEnded(object sender, CommandEventArgs e) => Queue("command.ended", e.GlobalCommandName, null);
        private static void OnCommandCancelled(object sender, CommandEventArgs e) => Queue("command.cancelled", e.GlobalCommandName, null);
        private static void OnCommandFailed(object sender, CommandEventArgs e) => Queue("command.failed", e.GlobalCommandName, null);

        private static void Queue(string eventType, string commandName, DBObject entity)
        {
            if (!_eventsEnabled) return;
            lock (Sync)
            {
                _eventType = eventType;
                if (!string.IsNullOrWhiteSpace(commandName)) _commandName = commandName;
                try
                {
                    if (entity != null && !entity.ObjectId.IsNull)
                        Handles.Add(entity.Handle.ToString());
                }
                catch { /* An erased object may no longer expose its handle. */ }
            }
        }

        private static void OnIdle(object sender, EventArgs e)
        {
            if (!_eventsEnabled) return;
            if (Interlocked.CompareExchange(ref _sending, 1, 0) != 0) return;
            string drawing;
            string eventType;
            string command;
            string[] handles;
            lock (Sync)
            {
                if (Handles.Count == 0 && _eventType == "document.changed")
                {
                    Interlocked.Exchange(ref _sending, 0);
                    return;
                }
                var document = CadApplication.DocumentManager.MdiActiveDocument;
                if (document == null)
                {
                    Interlocked.Exchange(ref _sending, 0);
                    return;
                }
                drawing = document.Name;
                eventType = _eventType;
                command = _commandName;
                handles = new string[Handles.Count];
                Handles.CopyTo(handles);
                Handles.Clear();
                _eventType = "document.changed";
                _commandName = "";
            }
            var sequence = Interlocked.Increment(ref _sequence);
            Task.Run(async () =>
            {
                try { await SendAsync(drawing, sequence, eventType, command, handles).ConfigureAwait(false); }
                catch { /* The MCP may be offline; polling remains available. */ }
                finally { Interlocked.Exchange(ref _sending, 0); }
            });
        }

        private static async Task SendAsync(string drawing, long sequence, string eventType, string command, string[] handles)
        {
            var tokenJson = await Http.GetStringAsync("api/editor/session").ConfigureAwait(false);
            var sessionObj = JObject.Parse(tokenJson);
            var token = sessionObj.Value<string>("token");
            if (string.IsNullOrWhiteSpace(token)) return;

            var payload = new JObject
            {
                ["drawing_name"] = drawing,
                ["sequence"] = sequence,
                ["event_type"] = eventType,
                ["command_name"] = command,
                ["changed_handles"] = new JArray(handles),
                ["origin"] = "autocad-plugin"
            };

            using (var request = new HttpRequestMessage(HttpMethod.Post, "api/editor/cad-event"))
            {
                request.Headers.Add("X-TopoSpatial-Token", token);
                request.Content = new StringContent(payload.ToString(Formatting.None), Encoding.UTF8, "application/json");
                await Http.SendAsync(request).ConfigureAwait(false);
            }
        }

        private static string Quote(string value) => "\"" + (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
    }
}
