using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEngine;

/// <summary>
/// Importing a device asset from the Device Generator pipeline into Unity.
///
/// The input is device.json and the texture.png atlas. The window cuts every part
/// out of the atlas into its own PNG, registers them as sprites and builds a
/// hierarchy in the scene: an empty object named after the device, holding a
/// SpriteRenderer per layer, placed by the positions from the JSON and spread along Z in teardown order.
///
/// Put it in an Editor folder (Assets/DeviceGenerator/Editor/, for instance).
/// Opened from: Tools → Device Asset Importer.
/// </summary>
public class DeviceAssetImporter : EditorWindow
{
    // ------------------------------------------------------------------
    // Window settings
    // ------------------------------------------------------------------

    const string PrefJson = "DeviceAssetImporter.json";
    const string PrefTexture = "DeviceAssetImporter.texture";
    const string PrefOut = "DeviceAssetImporter.out";
    const string PrefPpu = "DeviceAssetImporter.ppu";

    string _jsonPath = "";
    string _texturePath = "";
    string _outFolder = "Assets/Devices";      // always inside Assets
    float _pixelsPerUnit = 100f;
    float _zStep = 0.01f;
    int _sortingLayerIndex;
    int _sortingOrderBase;
    bool _makePrefab;
    bool _overwrite = true;
    FilterMode _filterMode = FilterMode.Bilinear;

    Vector2 _scroll;
    readonly List<string> _log = new List<string>();
    bool _lastRunFailed;

    [MenuItem("Tools/Device Asset Importer")]
    public static void Open()
    {
        var w = GetWindow<DeviceAssetImporter>(false, "Device Import", true);
        w.minSize = new Vector2(430, 460);
        w.Show();
    }

    void OnEnable()
    {
        _jsonPath = EditorPrefs.GetString(PrefJson, _jsonPath);
        _texturePath = EditorPrefs.GetString(PrefTexture, _texturePath);
        _outFolder = EditorPrefs.GetString(PrefOut, _outFolder);
        _pixelsPerUnit = EditorPrefs.GetFloat(PrefPpu, _pixelsPerUnit);
    }

    void OnDisable()
    {
        EditorPrefs.SetString(PrefJson, _jsonPath ?? "");
        EditorPrefs.SetString(PrefTexture, _texturePath ?? "");
        EditorPrefs.SetString(PrefOut, _outFolder ?? "");
        EditorPrefs.SetFloat(PrefPpu, _pixelsPerUnit);
    }

    // ------------------------------------------------------------------
    // Interface
    // ------------------------------------------------------------------

    void OnGUI()
    {
        _scroll = EditorGUILayout.BeginScrollView(_scroll);

        EditorGUILayout.LabelField("Device asset", EditorStyles.boldLabel);
        DrawDropArea();

        _jsonPath = PathField("device.json", _jsonPath, "json", false);
        _texturePath = PathField("Atlas (png)", _texturePath, "png", false);

        GUILayout.Space(6);
        EditorGUILayout.LabelField("Where to import", EditorStyles.boldLabel);
        DrawOutputField();

        GUILayout.Space(6);
        EditorGUILayout.LabelField("Assembly settings", EditorStyles.boldLabel);
        _pixelsPerUnit = Mathf.Max(1f, EditorGUILayout.FloatField(
            new GUIContent("Pixels Per Unit", "How many canvas pixels go into one Unity unit"),
            _pixelsPerUnit));
        _zStep = EditorGUILayout.FloatField(
            new GUIContent("Z step", "How much closer to the camera each next layer sits"),
            _zStep);

        var layerNames = SortingLayer.layers.Select(l => l.name).ToArray();
        _sortingLayerIndex = Mathf.Clamp(_sortingLayerIndex, 0, Mathf.Max(0, layerNames.Length - 1));
        if (layerNames.Length > 0)
        {
            _sortingLayerIndex = EditorGUILayout.Popup(
                new GUIContent("Sorting Layer", "Sorting layer for every part"),
                _sortingLayerIndex, layerNames);
        }
        _sortingOrderBase = EditorGUILayout.IntField(
            new GUIContent("Base order", "The part's layer is added to it"),
            _sortingOrderBase);
        _filterMode = (FilterMode)EditorGUILayout.EnumPopup("Filtering", _filterMode);

        _makePrefab = EditorGUILayout.Toggle(
            new GUIContent("Save prefab", "Put the assembled device down as a prefab next to the sprites"),
            _makePrefab);
        _overwrite = EditorGUILayout.Toggle(
            new GUIContent("Overwrite", "Import over an already existing device folder"),
            _overwrite);

        GUILayout.Space(10);
        using (new EditorGUI.DisabledScope(string.IsNullOrEmpty(_jsonPath)))
        {
            if (GUILayout.Button("Import", GUILayout.Height(32))) RunImport();
        }

        if (_log.Count > 0)
        {
            GUILayout.Space(8);
            EditorGUILayout.LabelField(_lastRunFailed ? "Errors" : "Report", EditorStyles.boldLabel);
            var style = new GUIStyle(EditorStyles.helpBox) { wordWrap = true, richText = false };
            EditorGUILayout.LabelField(string.Join("\n", _log), style);
        }

        EditorGUILayout.EndScrollView();
    }

    void DrawDropArea()
    {
        var rect = GUILayoutUtility.GetRect(0, 54, GUILayout.ExpandWidth(true));
        GUI.Box(rect, "Drop device.json and texture.png here\n(the whole asset folder works too)",
            EditorStyles.helpBox);

        var e = Event.current;
        if (!rect.Contains(e.mousePosition)) return;
        if (e.type != EventType.DragUpdated && e.type != EventType.DragPerform) return;

        DragAndDrop.visualMode = DragAndDropVisualMode.Copy;
        if (e.type != EventType.DragPerform) return;

        DragAndDrop.AcceptDrag();
        foreach (var p in DragAndDrop.paths) Accept(p);
        e.Use();
        Repaint();
    }

    /// <summary>Sort a dropped path into the fields. A folder is walked through.</summary>
    void Accept(string path)
    {
        if (string.IsNullOrEmpty(path)) return;
        var full = ToAbsolute(path);

        if (Directory.Exists(full))
        {
            foreach (var f in Directory.GetFiles(full)) Accept(f);
            return;
        }
        var ext = Path.GetExtension(full).ToLowerInvariant();
        if (ext == ".json")
        {
            // auxiliary jsons may sit next to it — we take the one that looks like an asset
            if (string.IsNullOrEmpty(_jsonPath) ||
                Path.GetFileName(full).ToLowerInvariant() == "device.json")
            {
                _jsonPath = full;
                AutoFillTexture();
            }
        }
        else if (ext == ".png")
        {
            var nm = Path.GetFileName(full).ToLowerInvariant();
            // the preview and the contact sheet are not the atlas
            if (nm.Contains("preview") || nm.Contains("contact") || nm.Contains("sheet")) return;
            _texturePath = full;
        }
    }

    /// <summary>The atlas is taken from the JSON's own texture field — it sits next to it.</summary>
    void AutoFillTexture()
    {
        try
        {
            var doc = MiniJson.Parse(File.ReadAllText(_jsonPath, Encoding.UTF8)) as Dictionary<string, object>;
            var rel = doc != null && doc.ContainsKey("texture") ? doc["texture"] as string : null;
            var dir = Path.GetDirectoryName(_jsonPath) ?? "";
            var guess = Path.GetFullPath(Path.Combine(dir, string.IsNullOrEmpty(rel) ? "texture.png" : rel));
            if (File.Exists(guess)) _texturePath = guess;
        }
        catch (Exception)
        {
            // silently: the user will point at the atlas by hand
        }
    }

    string PathField(string label, string value, string ext, bool folder)
    {
        EditorGUILayout.BeginHorizontal();
        EditorGUILayout.PrefixLabel(label);
        var shown = EditorGUILayout.TextField(value ?? "");
        if (GUILayout.Button("…", GUILayout.Width(26)))
        {
            var picked = folder
                ? EditorUtility.OpenFolderPanel(label, DirOf(value), "")
                : EditorUtility.OpenFilePanel(label, DirOf(value), ext);
            if (!string.IsNullOrEmpty(picked))
            {
                shown = picked;
                if (ext == "json") { _jsonPath = picked; AutoFillTexture(); shown = _jsonPath; }
            }
            GUI.FocusControl(null);
        }
        EditorGUILayout.EndHorizontal();
        return shown;
    }

    void DrawOutputField()
    {
        EditorGUILayout.BeginHorizontal();
        EditorGUILayout.PrefixLabel(new GUIContent("Folder", "Has to be inside Assets"));
        _outFolder = EditorGUILayout.TextField(_outFolder ?? "");
        if (GUILayout.Button("…", GUILayout.Width(26)))
        {
            var picked = EditorUtility.OpenFolderPanel("Where to import", Application.dataPath, "");
            if (!string.IsNullOrEmpty(picked))
            {
                var rel = ToProjectRelative(picked);
                if (rel == null) EditorUtility.DisplayDialog("No good",
                    "The folder has to be inside this project's Assets.", "OK");
                else _outFolder = rel;
            }
            GUI.FocusControl(null);
        }
        EditorGUILayout.EndHorizontal();
    }

    static string DirOf(string path)
    {
        if (string.IsNullOrEmpty(path)) return "";
        try { return Directory.Exists(path) ? path : (Path.GetDirectoryName(path) ?? ""); }
        catch (Exception) { return ""; }
    }

    // ------------------------------------------------------------------
    // Import
    // ------------------------------------------------------------------

    void RunImport()
    {
        _log.Clear();
        _lastRunFailed = false;
        try
        {
            Import();
        }
        catch (Exception ex)
        {
            _lastRunFailed = true;
            _log.Add("Import aborted: " + ex.Message);
            Debug.LogException(ex);
        }
        finally
        {
            EditorUtility.ClearProgressBar();
        }
        Repaint();
    }

    void Import()
    {
        // --- input checks ---
        if (string.IsNullOrEmpty(_jsonPath) || !File.Exists(_jsonPath))
            throw new Exception("device.json not found: " + _jsonPath);
        if (string.IsNullOrEmpty(_texturePath) || !File.Exists(_texturePath))
            throw new Exception("atlas not found: " + _texturePath);

        var outFolder = NormalizeOutFolder(_outFolder);

        var doc = DeviceDoc.Load(_jsonPath);
        if (doc.Parts.Count == 0) throw new Exception("the JSON has no parts");

        var atlas = LoadTexture(_texturePath);
        try
        {
            // The atlas size from the JSON is only a cross-check: we always cut by the file
            if (doc.TextureSize.x > 0 &&
                (Mathf.RoundToInt(doc.TextureSize.x) != atlas.width ||
                 Mathf.RoundToInt(doc.TextureSize.y) != atlas.height))
            {
                _log.Add(string.Format(
                    "Warning: the JSON says the atlas is {0}×{1}, the file is {2}×{3}. Cutting by the file — check this is the right atlas.",
                    Mathf.RoundToInt(doc.TextureSize.x), Mathf.RoundToInt(doc.TextureSize.y),
                    atlas.width, atlas.height));
            }

            var deviceFolder = outFolder + "/" + Sanitize(doc.Device);
            var spritesFolder = deviceFolder + "/Parts";
            var deviceFolderAbs = ToAbsolute(deviceFolder);

            if (Directory.Exists(deviceFolderAbs) && !_overwrite)
                throw new Exception("the folder already exists: " + deviceFolder + " — switch on \"Overwrite\"");

            EnsureFolder(spritesFolder);

            // --- cutting ---
            var written = new List<KeyValuePair<PartDef, string>>();  // part → sprite path
            var used = new HashSet<string>();
            for (int i = 0; i < doc.Parts.Count; i++)
            {
                var p = doc.Parts[i];
                EditorUtility.DisplayProgressBar("Importing the device",
                    "Cutting " + p.Id, (float)i / doc.Parts.Count);

                string err;
                var png = CutPart(atlas, p, out err);
                if (png == null) { _log.Add("Part skipped " + p.Id + ": " + err); continue; }

                var fileName = Sanitize(p.Id);
                var n = 1;
                while (!used.Add(fileName)) fileName = Sanitize(p.Id) + "_" + (++n);

                var assetPath = spritesFolder + "/" + fileName + ".png";
                File.WriteAllBytes(ToAbsolute(assetPath), png);
                written.Add(new KeyValuePair<PartDef, string>(p, assetPath));
            }
            if (written.Count == 0) throw new Exception("not a single part was cut");

            // re-import: PNGs of parts no longer in the new JSON are removed —
            // otherwise sprites of cut-out parts pile up in the folder
            var keep = new HashSet<string>(written.Select(kv => Path.GetFileName(kv.Value)));
            foreach (var stale in Directory.GetFiles(ToAbsolute(spritesFolder), "*.png"))
            {
                if (keep.Contains(Path.GetFileName(stale))) continue;
                AssetDatabase.DeleteAsset(spritesFolder + "/" + Path.GetFileName(stale));
                _log.Add("Stray sprite removed: " + Path.GetFileName(stale));
            }

            // --- register the PNGs as sprites ---
            EditorUtility.DisplayProgressBar("Importing the device", "Setting up the sprites", 0.7f);
            AssetDatabase.Refresh();

            var maxSide = written.Max(kv => Mathf.Max(kv.Key.Frame.width, kv.Key.Frame.height));
            var maxTex = MaxTextureSize(maxSide);
            foreach (var kv in written) ConfigureSprite(kv.Value, maxTex);
            AssetDatabase.Refresh();

            // --- assembling in the scene ---
            EditorUtility.DisplayProgressBar("Importing the device", "Assembling in the scene", 0.9f);
            var root = BuildHierarchy(doc, written);

            if (_makePrefab)
            {
                var prefabPath = deviceFolder + "/" + Sanitize(doc.Device) + ".prefab";
                PrefabUtility.SaveAsPrefabAssetAndConnect(root, prefabPath, InteractionMode.AutomatedAction);
                _log.Add("Prefab: " + prefabPath);
            }

            Selection.activeGameObject = root;
            EditorGUIUtility.PingObject(root);

            _log.Insert(0, string.Format("Done: {0} — {1} parts, sprites in {2}",
                doc.Device, written.Count, spritesFolder));
            _log.Add(string.Format("Canvas {0}×{1} px → {2:0.##}×{3:0.##} units at PPU {4:0.##}",
                Mathf.RoundToInt(doc.Canvas.x), Mathf.RoundToInt(doc.Canvas.y),
                doc.Canvas.x / _pixelsPerUnit, doc.Canvas.y / _pixelsPerUnit, _pixelsPerUnit));
        }
        finally
        {
            DestroyImmediate(atlas);
        }
    }

    /// <summary>Cut a part's frame out of the atlas into ready PNG bytes.</summary>
    byte[] CutPart(Texture2D atlas, PartDef p, out string error)
    {
        error = null;
        var f = p.Frame;
        if (f.width <= 0 || f.height <= 0) { error = "empty frame in the JSON"; return null; }

        // the JSON measures the atlas top-down, Unity bottom-up
        var x0 = Mathf.RoundToInt(f.x);
        var y0 = atlas.height - Mathf.RoundToInt(f.y + f.height);
        var w = Mathf.RoundToInt(f.width);
        var h = Mathf.RoundToInt(f.height);

        // clamp to the atlas bounds: better to return a truncated part than to crash
        var clampedX = Mathf.Clamp(x0, 0, atlas.width);
        var clampedY = Mathf.Clamp(y0, 0, atlas.height);
        w = Mathf.Min(w - (clampedX - x0), atlas.width - clampedX);
        h = Mathf.Min(h - (clampedY - y0), atlas.height - clampedY);
        if (w <= 0 || h <= 0) { error = "the frame is entirely outside the atlas"; return null; }
        if (clampedX != x0 || clampedY != y0 || w != Mathf.RoundToInt(f.width) || h != Mathf.RoundToInt(f.height))
            _log.Add("Part " + p.Id + ": the frame stuck out of the atlas, clamped to " + w + "×" + h);

        var pixels = atlas.GetPixels(clampedX, clampedY, w, h);
        var tex = new Texture2D(w, h, TextureFormat.RGBA32, false);
        try
        {
            tex.SetPixels(pixels);
            tex.Apply();
            return tex.EncodeToPNG();
        }
        finally
        {
            DestroyImmediate(tex);
        }
    }

    void ConfigureSprite(string assetPath, int maxTextureSize)
    {
        var ti = AssetImporter.GetAtPath(assetPath) as TextureImporter;
        if (ti == null) { _log.Add("Could not set up the sprite: " + assetPath); return; }

        ti.textureType = TextureImporterType.Sprite;
        ti.spriteImportMode = SpriteImportMode.Single;
        ti.spritePixelsPerUnit = _pixelsPerUnit;
        ti.mipmapEnabled = false;
        ti.alphaIsTransparency = true;
        ti.filterMode = _filterMode;
        ti.wrapMode = TextureWrapMode.Clamp;
        ti.maxTextureSize = maxTextureSize;
        ti.textureCompression = TextureImporterCompression.Uncompressed;

        // The pivot is strictly at the centre: in device.json position is the centre of
        // the part, and the pipeline viewer draws it the same way. Any other pivot would
        // break the match between the assembly in the game and in the viewer.
        var settings = new TextureImporterSettings();
        ti.ReadTextureSettings(settings);
        settings.spriteAlignment = (int)SpriteAlignment.Center;
        ti.SetTextureSettings(settings);

        ti.SaveAndReimport();
    }

    GameObject BuildHierarchy(DeviceDoc doc, List<KeyValuePair<PartDef, string>> written)
    {
        var sortingLayers = SortingLayer.layers;
        var sortingName = sortingLayers.Length > 0
            ? sortingLayers[Mathf.Clamp(_sortingLayerIndex, 0, sortingLayers.Length - 1)].name
            : "Default";

        var root = new GameObject(doc.Device);
        Undo.RegisterCreatedObjectUndo(root, "Importing the device");
        root.transform.position = Vector3.zero;

        // bottom-up: the order in the hierarchy matches the assembly order
        foreach (var kv in written.OrderBy(k => k.Key.Layer))
        {
            var p = kv.Key;
            var sprite = AssetDatabase.LoadAssetAtPath<Sprite>(kv.Value);
            if (sprite == null) { _log.Add("The sprite did not load: " + kv.Value); continue; }

            var go = new GameObject(string.IsNullOrEmpty(p.Name) ? p.Id : p.Name);
            go.transform.SetParent(root.transform, false);

            var sr = go.AddComponent<SpriteRenderer>();
            sr.sprite = sprite;
            sr.sortingLayerName = sortingName;
            sr.sortingOrder = _sortingOrderBase + p.Layer;

            // The canvas is measured top-down from the top-left corner, Unity bottom-up
            // from the centre. The canvas centre goes to the parent's origin.
            var x = (p.Position.x - doc.Canvas.x * 0.5f) / _pixelsPerUnit;
            var y = (doc.Canvas.y * 0.5f - p.Position.y) / _pixelsPerUnit;
            go.transform.localPosition = new Vector3(x, y, -p.Layer * _zStep);

            // rotation in the JSON is clockwise with the Y axis pointing down; in Unity
            // the Y axis points up, so clockwise there is a negative angle
            go.transform.localRotation = Quaternion.Euler(0f, 0f, -p.Rotation);

            // The frame in the atlas and the size on the canvas need not match (the part
            // may have been shrunk during assembly) — the difference is made up by scale.
            var sx = p.Frame.width > 0 ? p.Size.x * p.Scale / p.Frame.width : p.Scale;
            var sy = p.Frame.height > 0 ? p.Size.y * p.Scale / p.Frame.height : p.Scale;
            go.transform.localScale = new Vector3(sx, sy, 1f);
        }
        return root;
    }

    // ------------------------------------------------------------------
    // Paths and files
    // ------------------------------------------------------------------

    static Texture2D LoadTexture(string path)
    {
        // We read the file ourselves rather than through AssetDatabase: the atlas usually
        // lives outside the project, and the Read/Write flag on an imported texture is not needed.
        var tex = new Texture2D(2, 2, TextureFormat.RGBA32, false);
        if (!tex.LoadImage(File.ReadAllBytes(path)))
        {
            DestroyImmediate(tex);
            throw new Exception("could not read the atlas: " + path);
        }
        return tex;
    }

    string NormalizeOutFolder(string folder)
    {
        if (string.IsNullOrEmpty(folder)) throw new Exception("no destination folder was set");
        folder = folder.Replace('\\', '/').TrimEnd('/');

        if (Path.IsPathRooted(folder))
        {
            var rel = ToProjectRelative(folder);
            if (rel == null) throw new Exception("the destination folder has to be inside Assets: " + folder);
            folder = rel;
        }
        if (!folder.StartsWith("Assets", StringComparison.Ordinal))
            throw new Exception("the destination folder has to start with Assets/: " + folder);
        return folder;
    }

    static string ToProjectRelative(string absolute)
    {
        var data = Application.dataPath.Replace('\\', '/');
        var p = absolute.Replace('\\', '/');
        if (!p.StartsWith(data, StringComparison.OrdinalIgnoreCase)) return null;
        return "Assets" + p.Substring(data.Length);
    }

    static string ToAbsolute(string path)
    {
        if (string.IsNullOrEmpty(path)) return path;
        var p = path.Replace('\\', '/');
        if (Path.IsPathRooted(p)) return Path.GetFullPath(p);
        if (p.StartsWith("Assets", StringComparison.Ordinal))
            return Path.GetFullPath(Path.Combine(Application.dataPath, p.Substring("Assets".Length).TrimStart('/')));
        return Path.GetFullPath(p);
    }

    /// <summary>Create the chain of project folders so AssetDatabase knows about them.</summary>
    static void EnsureFolder(string projectPath)
    {
        var parts = projectPath.Split('/');
        var cur = parts[0];
        for (int i = 1; i < parts.Length; i++)
        {
            var next = cur + "/" + parts[i];
            if (!AssetDatabase.IsValidFolder(next)) AssetDatabase.CreateFolder(cur, parts[i]);
            cur = next;
        }
    }

    static string Sanitize(string name)
    {
        if (string.IsNullOrEmpty(name)) return "part";
        var sb = new StringBuilder(name.Length);
        foreach (var c in name)
            sb.Append(char.IsLetterOrDigit(c) || c == '_' || c == '-' ? c : '_');
        var res = sb.ToString().Trim('_');
        return res.Length == 0 ? "part" : res;
    }

    static int MaxTextureSize(float maxSide)
    {
        var size = 512;
        while (size < maxSide && size < 8192) size *= 2;
        return size;
    }

    // ------------------------------------------------------------------
    // The device.json model
    // ------------------------------------------------------------------

    class DeviceDoc
    {
        public string Device = "device";
        public Vector2 Canvas = new Vector2(1024, 1024);
        public Vector2 TextureSize = Vector2.zero;
        public readonly List<PartDef> Parts = new List<PartDef>();

        public static DeviceDoc Load(string path)
        {
            var root = MiniJson.Parse(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
            if (root == null) throw new Exception("device.json did not parse: the root is not an object");

            var doc = new DeviceDoc();
            doc.Device = MiniJson.Str(root, "device", "device");
            doc.Canvas = MiniJson.Vec2(root, "canvas", new Vector2(1024, 1024));
            doc.TextureSize = MiniJson.Vec2(root, "texture_size", Vector2.zero);

            var parts = MiniJson.Get(root, "parts") as List<object>;
            if (parts == null) throw new Exception("device.json has no parts array");

            var index = 0;
            foreach (var raw in parts)
            {
                var d = raw as Dictionary<string, object>;
                if (d == null) continue;
                var p = new PartDef
                {
                    Id = MiniJson.Str(d, "id", "part_" + index),
                    Name = MiniJson.Str(d, "name", null),
                    Type = MiniJson.Str(d, "type", "misc"),
                    Position = MiniJson.Vec2(d, "position", Vector2.zero),
                    Size = MiniJson.Vec2(d, "size", Vector2.zero),
                    Scale = MiniJson.Num(d, "scale", 1f),
                    Rotation = MiniJson.Num(d, "rotation", 0f),
                    Layer = Mathf.RoundToInt(MiniJson.Num(d, "layer", index)),
                };

                var frame = MiniJson.Get(d, "frame") as Dictionary<string, object>;
                if (frame != null)
                {
                    p.Frame = new Rect(
                        MiniJson.Num(frame, "x", 0f), MiniJson.Num(frame, "y", 0f),
                        MiniJson.Num(frame, "w", 0f), MiniJson.Num(frame, "h", 0f));
                }
                // size may be missing — then the part is drawn as it is in the atlas
                if (p.Size == Vector2.zero) p.Size = new Vector2(p.Frame.width, p.Frame.height);

                doc.Parts.Add(p);
                index++;
            }
            return doc;
        }
    }

    class PartDef
    {
        public string Id;
        public string Name;
        public string Type;
        public Rect Frame;
        public Vector2 Position;
        public Vector2 Size;
        public float Scale = 1f;
        public float Rotation;
        public int Layer;
    }

    // ------------------------------------------------------------------
    // JSON parsing
    // ------------------------------------------------------------------
    //
    // JsonUtility handles neither arrays of numbers at the root of a field nor nested
    // arrays like corners — and the pipeline format is made of them. Hence our own
    // small parser: objects, arrays, numbers, strings, true/false/null.

    static class MiniJson
    {
        public static object Parse(string text)
        {
            var i = 0;
            var value = ParseValue(text, ref i);
            SkipWhite(text, ref i);
            if (i < text.Length) throw new Exception("junk after the JSON at position " + i);
            return value;
        }

        public static object Get(Dictionary<string, object> d, string key)
        {
            object v;
            return d != null && d.TryGetValue(key, out v) ? v : null;
        }

        public static string Str(Dictionary<string, object> d, string key, string fallback)
        {
            var v = Get(d, key) as string;
            return string.IsNullOrEmpty(v) ? fallback : v;
        }

        public static float Num(Dictionary<string, object> d, string key, float fallback)
        {
            var v = Get(d, key);
            return v is double ? (float)(double)v : fallback;
        }

        public static Vector2 Vec2(Dictionary<string, object> d, string key, Vector2 fallback)
        {
            var list = Get(d, key) as List<object>;
            if (list == null || list.Count < 2) return fallback;
            var x = list[0] is double ? (float)(double)list[0] : fallback.x;
            var y = list[1] is double ? (float)(double)list[1] : fallback.y;
            return new Vector2(x, y);
        }

        static object ParseValue(string s, ref int i)
        {
            SkipWhite(s, ref i);
            if (i >= s.Length) throw new Exception("the JSON ended abruptly");
            switch (s[i])
            {
                case '{': return ParseObject(s, ref i);
                case '[': return ParseArray(s, ref i);
                case '"': return ParseString(s, ref i);
                case 't': Expect(s, ref i, "true"); return true;
                case 'f': Expect(s, ref i, "false"); return false;
                case 'n': Expect(s, ref i, "null"); return null;
                default: return ParseNumber(s, ref i);
            }
        }

        static Dictionary<string, object> ParseObject(string s, ref int i)
        {
            var res = new Dictionary<string, object>();
            i++;                                    // '{'
            SkipWhite(s, ref i);
            if (i < s.Length && s[i] == '}') { i++; return res; }
            while (true)
            {
                SkipWhite(s, ref i);
                if (i >= s.Length || s[i] != '"') throw new Exception("expected a key at position " + i);
                var key = ParseString(s, ref i);
                SkipWhite(s, ref i);
                if (i >= s.Length || s[i] != ':') throw new Exception("expected ':' at position " + i);
                i++;
                res[key] = ParseValue(s, ref i);
                SkipWhite(s, ref i);
                if (i >= s.Length) throw new Exception("the object was not closed");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == '}') { i++; return res; }
                throw new Exception("expected ',' or '}' at position " + i);
            }
        }

        static List<object> ParseArray(string s, ref int i)
        {
            var res = new List<object>();
            i++;                                    // '['
            SkipWhite(s, ref i);
            if (i < s.Length && s[i] == ']') { i++; return res; }
            while (true)
            {
                res.Add(ParseValue(s, ref i));
                SkipWhite(s, ref i);
                if (i >= s.Length) throw new Exception("the array was not closed");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == ']') { i++; return res; }
                throw new Exception("expected ',' or ']' at position " + i);
            }
        }

        static string ParseString(string s, ref int i)
        {
            var sb = new StringBuilder();
            i++;                                    // '"'
            while (i < s.Length)
            {
                var c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                if (i >= s.Length) break;
                var esc = s[i++];
                switch (esc)
                {
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'u':
                        if (i + 4 > s.Length) throw new Exception("truncated \\u");
                        sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16));
                        i += 4;
                        break;
                    default: throw new Exception("unknown escape \\" + esc);
                }
            }
            throw new Exception("the string was not closed");
        }

        static object ParseNumber(string s, ref int i)
        {
            var start = i;
            while (i < s.Length && "+-.eE0123456789".IndexOf(s[i]) >= 0) i++;
            var text = s.Substring(start, i - start);
            double d;
            if (!double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out d))
                throw new Exception("not a number: '" + text + "' at position " + start);
            return d;
        }

        static void Expect(string s, ref int i, string word)
        {
            if (i + word.Length > s.Length || s.Substring(i, word.Length) != word)
                throw new Exception("expected '" + word + "' at position " + i);
            i += word.Length;
        }

        static void SkipWhite(string s, ref int i)
        {
            while (i < s.Length && char.IsWhiteSpace(s[i])) i++;
        }
    }
}
