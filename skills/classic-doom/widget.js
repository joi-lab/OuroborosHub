/**
 * Classic Doom (1993) - Standalone Ouroboros Extension Widget
 * Self-contained 2.5D Raycaster FPS Engine & Synthesizer
 */

(function() {
'use strict';
var DOOM = {};


/* --- File: 00_util.js --- */

/* ==========================================================================
 * DOOM :: util.js -- core math, RNG, palette and colormap (light diminishing)
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var PI = Math.PI;
    var PI2 = PI * 2;

    function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
    function lerp(a, b, t) { return a + (b - a) * t; }

    /** Wrap an angle into [0, 2*PI). */
    function normAngle(a) {
        a = a % PI2;
        if (a < 0) a += PI2;
        return a;
    }

    /** Shortest signed delta from angle b to angle a, in (-PI, PI]. */
    function angleDiff(a, b) {
        var d = normAngle(a - b);
        if (d > PI) d -= PI2;
        return d;
    }

    function dist2(ax, ay, bx, by) {
        var dx = bx - ax, dy = by - ay;
        return dx * dx + dy * dy;
    }

    function dist(ax, ay, bx, by) { return Math.sqrt(dist2(ax, ay, bx, by)); }

    /** Deterministic 32-bit PRNG (mulberry32). Same seed => same textures. */
    function makeRng(seed) {
        var s = seed >>> 0;
        return function () {
            s = (s + 0x6D2B79F5) >>> 0;
            var t = s;
            t = Math.imul(t ^ (t >>> 15), 1 | t);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    // ---------------------------------------------------------------- palette
    //
    // Doom stored graphics as 8-bit indices into a 256 colour palette, and did
    // its depth shading with a "colormap": 32 pre-darkened copies of that
    // palette. We reproduce exactly that -- it is both authentic and fast,
    // since a shaded pixel costs one array lookup.
    //
    // The palette is 15 ramps of 16 shades (dark -> bright) plus specials.

    var RAMP = {
        GREY: 0, BROWN: 1, RED: 2, GREEN: 3, BLUE: 4, YELLOW: 5,
        FIRE: 6, FLESH: 7, STEEL: 8, PURPLE: 9, OOZE: 10, BONE: 11,
        BLOOD: 12, PLASMA: 13, OLIVE: 14
    };

    /** Palette index for ramp `r` (RAMP.*) at brightness `s` in 0..15. */
    function C(r, s) { return (r << 4) | (s < 0 ? 0 : (s > 15 ? 15 : s | 0)); }

    var TRANSPARENT = 255;

    // Ramp end points: [darkR,darkG,darkB, brightR,brightG,brightB]
    var RAMP_DEFS = [
        [12, 12, 14, 231, 231, 235],   // GREY   - stone / concrete
        [26, 15, 6, 219, 168, 108],    // BROWN  - brick, dirt, wood trim
        [40, 0, 0, 255, 90, 66],       // RED    - keycards, hell trim
        [0, 22, 4, 92, 235, 96],       // GREEN  - armour, nukage highlight
        [0, 8, 34, 92, 148, 255],      // BLUE   - keycards, plasma armour
        [40, 32, 0, 255, 236, 104],    // YELLOW - keycards, lights
        [48, 12, 0, 255, 214, 92],     // FIRE   - muzzle flash, fireballs
        [40, 18, 18, 255, 186, 168],   // FLESH  - imp/skin, faces
        [10, 14, 18, 198, 214, 231],   // STEEL  - metal, chrome
        [22, 6, 30, 190, 120, 236],    // PURPLE - hell marble veins
        [4, 18, 0, 176, 231, 40],      // OOZE   - radioactive slime
        [34, 30, 20, 246, 240, 210],   // BONE   - skulls, bone, sky haze
        [22, 0, 0, 168, 20, 20],       // BLOOD  - gore, dark red
        [0, 24, 40, 150, 244, 255],    // PLASMA - plasma bolts, screens
        [16, 16, 6, 150, 148, 74]      // OLIVE  - zombie uniforms
    ];

    var PALETTE = new Uint8Array(256 * 3);
    (function buildPalette() {
        var i, s, d, o;
        for (i = 0; i < RAMP_DEFS.length; i++) {
            d = RAMP_DEFS[i];
            for (s = 0; s < 16; s++) {
                var t = s / 15;
                // slight gamma so mid tones stay punchy like the original
                var g = Math.pow(t, 0.85);
                o = ((i << 4) | s) * 3;
                PALETTE[o] = clamp(Math.round(lerp(d[0], d[3], g)), 0, 255);
                PALETTE[o + 1] = clamp(Math.round(lerp(d[1], d[4], g)), 0, 255);
                PALETTE[o + 2] = clamp(Math.round(lerp(d[2], d[5], g)), 0, 255);
            }
        }
        // 240..254: specials (pure black, pure white, HUD accents)
        var specials = [
            [0, 0, 0], [255, 255, 255], [255, 0, 0], [0, 255, 0], [0, 0, 255],
            [255, 255, 0], [255, 128, 0], [128, 0, 255], [0, 255, 255],
            [255, 0, 255], [96, 0, 0], [0, 96, 0], [0, 0, 96], [64, 64, 64],
            [176, 176, 176]
        ];
        for (i = 0; i < specials.length; i++) {
            o = (240 + i) * 3;
            PALETTE[o] = specials[i][0];
            PALETTE[o + 1] = specials[i][1];
            PALETTE[o + 2] = specials[i][2];
        }
        // 255 is the transparency key; keep it magenta so bugs are obvious.
        PALETTE[255 * 3] = 255; PALETTE[255 * 3 + 1] = 0; PALETTE[255 * 3 + 2] = 255;
    })();

    var LIGHT_LEVELS = 32;

    /**
     * Colormap: LIGHT_LEVELS shaded copies of the palette, pre-packed as ABGR
     * 32-bit words ready to be written straight into an ImageData buffer.
     * Level 0 is full bright, level 31 is black.
     */
    var COLORMAP = new Uint32Array(LIGHT_LEVELS * 256);
    (function buildColormap() {
        for (var l = 0; l < LIGHT_LEVELS; l++) {
            // Doom's shading is not linear: it falls off gently then plunges.
            var f = 1 - l / (LIGHT_LEVELS - 1);
            f = Math.pow(f, 1.35);
            for (var i = 0; i < 256; i++) {
                var o = i * 3;
                var r = (PALETTE[o] * f) | 0;
                var g = (PALETTE[o + 1] * f) | 0;
                var b = (PALETTE[o + 2] * f) | 0;
                COLORMAP[(l << 8) | i] = (255 << 24) | (b << 16) | (g << 8) | r;
            }
        }
    })();

    /** Fully saturated (unshaded) ABGR word for a palette index. */
    function paletteABGR(index) { return COLORMAP[index & 255]; }

    /** CSS colour string for a palette index -- used by the HUD and menus. */
    function paletteCSS(index, alpha) {
        var o = (index & 255) * 3;
        var a = (alpha === undefined) ? 1 : alpha;
        return 'rgba(' + PALETTE[o] + ',' + PALETTE[o + 1] + ',' + PALETTE[o + 2] + ',' + a + ')';
    }

    /**
     * Map a distance to a colormap light level, the way Doom's
     * "diminishing lighting" works: brightness falls off with distance and
     * with the sector's own light level.
     *
     * @param {number} d          distance in map units
     * @param {number} sectorLight 0 (pitch black) .. 1 (fully lit)
     * @param {number} [bias]     extra darkening, e.g. for shaded wall sides
     */
    function lightForDistance(d, sectorLight, bias) {
        var lvl = d * 1.55;                      // distance falloff
        lvl += (1 - sectorLight) * 22;           // sector darkness
        if (bias) lvl += bias;
        if (lvl < 0) lvl = 0;
        if (lvl > LIGHT_LEVELS - 1) lvl = LIGHT_LEVELS - 1;
        return lvl | 0;
    }

    DOOM.Util = {
        PI: PI,
        PI2: PI2,
        RAMP: RAMP,
        C: C,
        TRANSPARENT: TRANSPARENT,
        PALETTE: PALETTE,
        COLORMAP: COLORMAP,
        LIGHT_LEVELS: LIGHT_LEVELS,
        clamp: clamp,
        lerp: lerp,
        normAngle: normAngle,
        angleDiff: angleDiff,
        dist: dist,
        dist2: dist2,
        makeRng: makeRng,
        paletteABGR: paletteABGR,
        paletteCSS: paletteCSS,
        lightForDistance: lightForDistance
    };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 01_raster.js --- */

/* ==========================================================================
 * DOOM :: raster.js -- tiny software rasteriser onto 8-bit indexed buffers
 *
 * Every texture and sprite in the game is drawn by code through this, so the
 * widget ships with zero image assets and stays byte-for-byte self contained.
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;

    /**
     * @param {number} w
     * @param {number} h
     * @param {number} [fill] palette index to clear with (default transparent)
     */
    function Raster(w, h, fill) {
        this.w = w;
        this.h = h;
        this.px = new Uint8Array(w * h);
        if (fill === undefined) fill = U.TRANSPARENT;
        if (fill !== 0) this.px.fill(fill);
    }

    Raster.prototype.get = function (x, y) {
        if (x < 0 || y < 0 || x >= this.w || y >= this.h) return U.TRANSPARENT;
        return this.px[y * this.w + x];
    };

    Raster.prototype.set = function (x, y, c) {
        if (x < 0 || y < 0 || x >= this.w || y >= this.h) return;
        this.px[(y | 0) * this.w + (x | 0)] = c;
    };

    /** Set without bounds checking; caller guarantees the coordinates. */
    Raster.prototype.setFast = function (x, y, c) { this.px[y * this.w + x] = c; };

    Raster.prototype.clear = function (c) { this.px.fill(c); };

    Raster.prototype.rect = function (x, y, w, h, c) {
        var x0 = Math.max(0, x | 0), y0 = Math.max(0, y | 0);
        var x1 = Math.min(this.w, (x + w) | 0), y1 = Math.min(this.h, (y + h) | 0);
        for (var yy = y0; yy < y1; yy++) {
            var row = yy * this.w;
            for (var xx = x0; xx < x1; xx++) this.px[row + xx] = c;
        }
    };

    /** Rectangle outline of the given thickness. */
    Raster.prototype.frame = function (x, y, w, h, c, t) {
        t = t || 1;
        this.rect(x, y, w, t, c);
        this.rect(x, y + h - t, w, t, c);
        this.rect(x, y, t, h, c);
        this.rect(x + w - t, y, t, h, c);
    };

    /** Axis-aligned filled ellipse inscribed in the given box. */
    Raster.prototype.ellipse = function (cx, cy, rx, ry, c) {
        if (rx <= 0 || ry <= 0) return;
        var y0 = Math.max(0, Math.ceil(cy - ry)), y1 = Math.min(this.h - 1, Math.floor(cy + ry));
        for (var yy = y0; yy <= y1; yy++) {
            var dy = (yy - cy) / ry;
            var s = 1 - dy * dy;
            if (s <= 0) continue;
            var hw = rx * Math.sqrt(s);
            var x0 = Math.max(0, Math.ceil(cx - hw)), x1 = Math.min(this.w - 1, Math.floor(cx + hw));
            var row = yy * this.w;
            for (var xx = x0; xx <= x1; xx++) this.px[row + xx] = c;
        }
    };

    Raster.prototype.circle = function (cx, cy, r, c) { this.ellipse(cx, cy, r, r, c); };

    Raster.prototype.line = function (x0, y0, x1, y1, c) {
        x0 |= 0; y0 |= 0; x1 |= 0; y1 |= 0;
        var dx = Math.abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
        var dy = -Math.abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
        var err = dx + dy;
        for (;;) {
            this.set(x0, y0, c);
            if (x0 === x1 && y0 === y1) break;
            var e2 = 2 * err;
            if (e2 >= dy) { err += dy; x0 += sx; }
            if (e2 <= dx) { err += dx; y0 += sy; }
        }
    };

    /** Filled convex/concave polygon via scanline parity fill. */
    Raster.prototype.poly = function (pts, c) {
        var i, minY = Infinity, maxY = -Infinity;
        for (i = 0; i < pts.length; i++) {
            if (pts[i][1] < minY) minY = pts[i][1];
            if (pts[i][1] > maxY) maxY = pts[i][1];
        }
        var y0 = Math.max(0, Math.ceil(minY)), y1 = Math.min(this.h - 1, Math.floor(maxY));
        var xs = [];
        for (var yy = y0; yy <= y1; yy++) {
            xs.length = 0;
            var sy = yy + 0.5;
            for (i = 0; i < pts.length; i++) {
                var a = pts[i], b = pts[(i + 1) % pts.length];
                if ((a[1] <= sy && b[1] > sy) || (b[1] <= sy && a[1] > sy)) {
                    xs.push(a[0] + (sy - a[1]) / (b[1] - a[1]) * (b[0] - a[0]));
                }
            }
            xs.sort(function (p, q) { return p - q; });
            for (i = 0; i + 1 < xs.length; i += 2) {
                var lx = Math.max(0, Math.ceil(xs[i])), rx = Math.min(this.w - 1, Math.floor(xs[i + 1]));
                var row = yy * this.w;
                for (var xx = lx; xx <= rx; xx++) this.px[row + xx] = c;
            }
        }
    };

    /**
     * Vertical gradient over a box, walking a ramp from shade `s0` to `s1`.
     * The workhorse for making flat code-drawn shapes look lit.
     */
    Raster.prototype.vgrad = function (x, y, w, h, ramp, s0, s1) {
        for (var yy = 0; yy < h; yy++) {
            var s = Math.round(U.lerp(s0, s1, h === 1 ? 0 : yy / (h - 1)));
            this.rect(x, y + yy, w, 1, U.C(ramp, s));
        }
    };

    Raster.prototype.hgrad = function (x, y, w, h, ramp, s0, s1) {
        for (var xx = 0; xx < w; xx++) {
            var s = Math.round(U.lerp(s0, s1, w === 1 ? 0 : xx / (w - 1)));
            this.rect(x + xx, y, 1, h, U.C(ramp, s));
        }
    };

    /**
     * Perturb the brightness of every non-transparent pixel in a box, keeping
     * it inside its own ramp. This is what gives code-drawn surfaces grain.
     */
    Raster.prototype.speckle = function (x, y, w, h, rng, amount, skipTransparent) {
        var x0 = Math.max(0, x | 0), y0 = Math.max(0, y | 0);
        var x1 = Math.min(this.w, (x + w) | 0), y1 = Math.min(this.h, (y + h) | 0);
        for (var yy = y0; yy < y1; yy++) {
            var row = yy * this.w;
            for (var xx = x0; xx < x1; xx++) {
                var c = this.px[row + xx];
                if (skipTransparent !== false && c === U.TRANSPARENT) continue;
                if (c >= 240) continue;                 // leave specials alone
                var ramp = c >> 4, s = c & 15;
                s += Math.round((rng() * 2 - 1) * amount);
                this.px[row + xx] = U.C(ramp, s);
            }
        }
    };

    /** Brighten/darken every opaque pixel in a box by a fixed number of shades. */
    Raster.prototype.adjust = function (x, y, w, h, delta) {
        var x0 = Math.max(0, x | 0), y0 = Math.max(0, y | 0);
        var x1 = Math.min(this.w, (x + w) | 0), y1 = Math.min(this.h, (y + h) | 0);
        for (var yy = y0; yy < y1; yy++) {
            var row = yy * this.w;
            for (var xx = x0; xx < x1; xx++) {
                var c = this.px[row + xx];
                if (c === U.TRANSPARENT || c >= 240) continue;
                this.px[row + xx] = U.C(c >> 4, (c & 15) + delta);
            }
        }
    };

    /** Blit another raster, honouring the transparency key. */
    Raster.prototype.blit = function (src, dx, dy) {
        for (var yy = 0; yy < src.h; yy++) {
            var ty = dy + yy;
            if (ty < 0 || ty >= this.h) continue;
            var srow = yy * src.w, trow = ty * this.w;
            for (var xx = 0; xx < src.w; xx++) {
                var c = src.px[srow + xx];
                if (c === U.TRANSPARENT) continue;
                var tx = dx + xx;
                if (tx < 0 || tx >= this.w) continue;
                this.px[trow + tx] = c;
            }
        }
    };

    /** Mirror horizontally into a new raster. */
    Raster.prototype.mirrored = function () {
        var r = new Raster(this.w, this.h, 0);
        for (var yy = 0; yy < this.h; yy++) {
            var row = yy * this.w;
            for (var xx = 0; xx < this.w; xx++) r.px[row + this.w - 1 - xx] = this.px[row + xx];
        }
        return r;
    };

    /**
     * Tileable value noise, returned as a function (x, y) -> 0..1.
     * Used for stone grain, rust, and slime turbulence.
     */
    function tileNoise(size, cells, rng) {
        var g = new Float32Array(cells * cells);
        for (var i = 0; i < g.length; i++) g[i] = rng();
        var scale = cells / size;
        return function (x, y) {
            var fx = x * scale, fy = y * scale;
            var x0 = Math.floor(fx), y0 = Math.floor(fy);
            var tx = fx - x0, ty = fy - y0;
            tx = tx * tx * (3 - 2 * tx);
            ty = ty * ty * (3 - 2 * ty);
            var xa = ((x0 % cells) + cells) % cells, xb = (xa + 1) % cells;
            var ya = ((y0 % cells) + cells) % cells, yb = (ya + 1) % cells;
            var v00 = g[ya * cells + xa], v10 = g[ya * cells + xb];
            var v01 = g[yb * cells + xa], v11 = g[yb * cells + xb];
            return U.lerp(U.lerp(v00, v10, tx), U.lerp(v01, v11, tx), ty);
        };
    }

    /** Sum of octaves of tileNoise, still perfectly tileable. */
    function fbm(size, baseCells, octaves, rng) {
        var layers = [], amp = [], total = 0;
        for (var o = 0; o < octaves; o++) {
            layers.push(tileNoise(size, baseCells * Math.pow(2, o), rng));
            var a = Math.pow(0.5, o);
            amp.push(a);
            total += a;
        }
        return function (x, y) {
            var v = 0;
            for (var o = 0; o < layers.length; o++) v += layers[o](x, y) * amp[o];
            return v / total;
        };
    }

    DOOM.Raster = Raster;
    DOOM.tileNoise = tileNoise;
    DOOM.fbm = fbm;

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 02_textures.js --- */

/* ==========================================================================
 * DOOM :: textures.js -- procedural wall textures, floor flats and sky
 *
 * All textures are 64x64 8-bit indexed buffers, mirroring the original WAD
 * format. Flats that animate (nukage, blood) expose several frames.
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;
    var C = U.C, R = U.RAMP;
    var Raster = DOOM.Raster;
    var fbm = DOOM.fbm;

    var TS = 64;               // texture size, matching Doom's flats

    // Wall texture ids ------------------------------------------------------
    var TEX = {
        STONE: 0, TECH: 1, METAL: 2, COMPUTER: 3, NUKEWALL: 4, EXITSIGN: 5,
        DOOR: 6, DOORTRAK: 7, MARBLE: 8, SKIN: 9, WOOD: 10, BRICK: 11,
        DOOR_RED: 12, DOOR_BLUE: 13, DOOR_YELLOW: 14, SWITCH: 15,
        SUPPORT: 16, GSTONE: 17, SKULLWALL: 18, BARS: 19
    };

    // Flat ids --------------------------------------------------------------
    var FLAT = {
        FLOOR: 0, TECHFLOOR: 1, NUKAGE: 2, BLOOD: 3, GRATE: 4,
        CEIL: 5, CEILLIGHT: 6, PENTAGRAM: 7, HELLROCK: 8, HELLCEIL: 9
    };
    var SKY_FLAT = 255;

    // ------------------------------------------------------------------ walls

    function texStone(rng) {
        var t = new Raster(TS, TS, C(R.GREY, 7));
        var n = fbm(TS, 4, 4, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) {
                var v = n(x, y);
                t.setFast(x, y, C(R.GREY, 4 + Math.round(v * 7)));
            }
        }
        // Chiselled block courses, offset every other row like real masonry.
        var bh = 16, bw = 32;
        for (var by = 0; by < TS; by += bh) {
            var off = ((by / bh) & 1) ? bw / 2 : 0;
            t.rect(0, by, TS, 1, C(R.GREY, 2));
            t.rect(0, by + 1, TS, 1, C(R.GREY, 11));
            for (var bx = 0; bx < TS; bx += bw) {
                var mx = (bx + off) % TS;
                t.rect(mx, by, 1, bh, C(R.GREY, 2));
                t.rect((mx + 1) % TS, by, 1, bh, C(R.GREY, 10));
            }
        }
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function texTech(rng) {
        // STARTAN-style: light grey band, brown centre panel, riveted edges.
        var t = new Raster(TS, TS, C(R.GREY, 8));
        t.vgrad(0, 0, TS, TS, R.GREY, 9, 5);
        t.rect(0, 0, TS, 3, C(R.GREY, 11));
        t.rect(0, TS - 3, TS, 3, C(R.GREY, 2));
        t.rect(8, 6, TS - 16, TS - 12, C(R.BROWN, 7));
        t.vgrad(8, 6, TS - 16, TS - 12, R.BROWN, 9, 4);
        t.frame(8, 6, TS - 16, TS - 12, C(R.BROWN, 2), 1);
        t.frame(7, 5, TS - 14, TS - 10, C(R.GREY, 12), 1);
        // horizontal seam plus rivets
        t.rect(8, TS / 2 - 1, TS - 16, 2, C(R.BROWN, 3));
        var rivets = [[4, 5], [4, TS - 6], [TS - 5, 5], [TS - 5, TS - 6], [4, TS / 2], [TS - 5, TS / 2]];
        for (var i = 0; i < rivets.length; i++) {
            t.circle(rivets[i][0], rivets[i][1], 2, C(R.GREY, 12));
            t.set(rivets[i][0] - 1, rivets[i][1] - 1, C(R.GREY, 15));
        }
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function texMetal(rng) {
        var t = new Raster(TS, TS, C(R.STEEL, 6));
        t.vgrad(0, 0, TS, TS, R.STEEL, 8, 4);
        // vertical corrugation
        for (var x = 0; x < TS; x += 8) {
            t.rect(x, 0, 1, TS, C(R.STEEL, 10));
            t.rect(x + 1, 0, 2, TS, C(R.STEEL, 7));
            t.rect(x + 6, 0, 2, TS, C(R.STEEL, 2));
        }
        t.rect(0, 0, TS, 4, C(R.STEEL, 9));
        t.rect(0, TS - 4, TS, 4, C(R.STEEL, 2));
        // rust streaks
        var n = fbm(TS, 3, 3, rng);
        for (var y = 0; y < TS; y++) {
            for (var xx = 0; xx < TS; xx++) {
                if (n(xx, y * 0.35) > 0.72) t.setFast(xx, y, C(R.BROWN, 3 + ((y >> 3) & 3)));
            }
        }
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function texComputer(rng) {
        var t = new Raster(TS, TS, C(R.STEEL, 3));
        t.vgrad(0, 0, TS, TS, R.STEEL, 5, 2);
        t.frame(0, 0, TS, TS, C(R.STEEL, 7), 2);
        // two banks of blinking screens with scrolling "data"
        for (var b = 0; b < 2; b++) {
            var by = 6 + b * 30;
            t.rect(6, by, TS - 12, 22, C(R.PLASMA, 1));
            t.frame(6, by, TS - 12, 22, C(R.STEEL, 8), 1);
            for (var ln = 0; ln < 5; ln++) {
                var y = by + 3 + ln * 4;
                var x = 9;
                while (x < TS - 10) {
                    var w = 2 + ((rng() * 7) | 0);
                    if (x + w > TS - 10) w = TS - 10 - x;
                    if (rng() > 0.32) {
                        t.rect(x, y, w, 2, C(rng() > 0.75 ? R.GREEN : R.PLASMA, 9 + ((rng() * 6) | 0)));
                    }
                    x += w + 2;
                }
            }
        }
        // status LEDs down the side
        for (var i = 0; i < 6; i++) {
            t.rect(1, 4 + i * 10, 3, 4, C(i % 3 === 0 ? R.RED : (i % 3 === 1 ? R.YELLOW : R.GREEN), 12));
        }
        return t;
    }

    function texNukeWall(rng) {
        var t = texMetal(rng);
        var n = fbm(TS, 4, 3, rng);
        // radioactive staining that pools towards the bottom
        for (var y = 0; y < TS; y++) {
            var bias = y / TS;
            for (var x = 0; x < TS; x++) {
                if (n(x, y) + bias * 0.55 > 0.78) t.setFast(x, y, C(R.OOZE, 4 + Math.round(n(x, y) * 9)));
            }
        }
        t.rect(0, TS - 6, TS, 6, C(R.OOZE, 6));
        t.speckle(0, TS - 6, TS, 6, rng, 3);
        // hazard chevrons
        for (var i = -TS; i < TS; i += 12) {
            t.poly([[i, 18], [i + 6, 18], [i + 14, 30], [i + 8, 30]], C(R.YELLOW, 11));
        }
        return t;
    }

    function texExitSign(rng) {
        var t = new Raster(TS, TS, C(R.STEEL, 3));
        t.vgrad(0, 0, TS, TS, R.STEEL, 4, 2);
        t.rect(4, 18, TS - 8, 28, C(R.GREEN, 2));
        t.frame(4, 18, TS - 8, 28, C(R.GREEN, 12), 2);
        // block letters E X I T
        var g = C(R.GREEN, 14);
        function stroke(x, y, w, h) { t.rect(x, y, w, h, g); }
        // E
        stroke(10, 25, 3, 14); stroke(10, 25, 9, 3); stroke(10, 30, 7, 3); stroke(10, 36, 9, 3);
        // X
        t.line(22, 25, 30, 38, g); t.line(23, 25, 31, 38, g);
        t.line(30, 25, 22, 38, g); t.line(31, 25, 23, 38, g);
        // I
        stroke(35, 25, 3, 14);
        // T
        stroke(42, 25, 12, 3); stroke(46, 25, 3, 14);
        t.speckle(0, 0, TS, 18, rng, 1);
        return t;
    }

    function texDoor(rng, trimRamp) {
        var t = new Raster(TS, TS, C(R.STEEL, 5));
        t.vgrad(0, 0, TS, TS, R.STEEL, 7, 3);
        // heavy centre seam -- the door "splits" visually even though it lifts
        t.rect(0, 0, 5, TS, C(R.STEEL, 8));
        t.rect(TS - 5, 0, 5, TS, C(R.STEEL, 8));
        t.rect(TS / 2 - 1, 0, 2, TS, C(R.STEEL, 1));
        for (var y = 6; y < TS - 6; y += 10) {
            t.rect(6, y, TS - 12, 6, C(R.STEEL, 6));
            t.rect(6, y, TS - 12, 1, C(R.STEEL, 10));
            t.rect(6, y + 5, TS - 12, 1, C(R.STEEL, 2));
        }
        if (trimRamp !== undefined) {
            // keycard-locked doors get a coloured stripe and a card slot
            t.rect(0, 0, TS, 6, C(trimRamp, 10));
            t.rect(0, TS - 6, TS, 6, C(trimRamp, 10));
            t.rect(TS / 2 - 5, 26, 10, 12, C(trimRamp, 13));
            t.frame(TS / 2 - 5, 26, 10, 12, C(trimRamp, 4), 1);
        }
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function texDoorTrack(rng) {
        var t = new Raster(TS, TS, C(R.STEEL, 2));
        t.hgrad(0, 0, TS, TS, R.STEEL, 1, 5);
        for (var x = 0; x < TS; x += 6) t.rect(x, 0, 2, TS, C(R.STEEL, 1));
        t.rect(0, 0, 2, TS, C(R.STEEL, 6));
        t.rect(TS - 2, 0, 2, TS, C(R.STEEL, 6));
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function texMarble(rng) {
        var t = new Raster(TS, TS, C(R.GREY, 6));
        var n = fbm(TS, 3, 4, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) {
                var v = n(x, y);
                // veins: the ridged-noise trick, tinted purple like hell marble
                var vein = Math.abs(v - 0.5) * 2;
                var c = vein < 0.14 ? C(R.PURPLE, 5 + Math.round(vein * 30))
                    : C(R.GREY, 3 + Math.round(v * 8));
                t.setFast(x, y, c);
            }
        }
        t.rect(0, 0, TS, 2, C(R.GREY, 9));
        t.rect(0, TS - 2, TS, 2, C(R.GREY, 1));
        return t;
    }

    function texSkin(rng) {
        var t = new Raster(TS, TS, C(R.FLESH, 6));
        var n = fbm(TS, 5, 3, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) {
                t.setFast(x, y, C(R.FLESH, 3 + Math.round(n(x, y) * 8)));
            }
        }
        // stretched faces / sinew
        for (var i = 0; i < 6; i++) {
            var cx = rng() * TS, cy = rng() * TS;
            t.ellipse(cx, cy, 5 + rng() * 5, 7 + rng() * 6, C(R.BLOOD, 5 + ((rng() * 5) | 0)));
            t.ellipse(cx - 3, cy - 2, 1.5, 2, C(R.BLOOD, 1));
            t.ellipse(cx + 3, cy - 2, 1.5, 2, C(R.BLOOD, 1));
            t.ellipse(cx, cy + 4, 3, 1.5, C(R.BLOOD, 1));
        }
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function texWood(rng) {
        var t = new Raster(TS, TS, C(R.BROWN, 6));
        var n = fbm(TS, 2, 3, rng);
        for (var x = 0; x < TS; x++) {
            for (var y = 0; y < TS; y++) {
                var grain = Math.sin(x * 0.7 + n(x, y) * 9) * 0.5 + 0.5;
                t.setFast(x, y, C(R.BROWN, 4 + Math.round(grain * 6)));
            }
        }
        for (var p = 0; p < TS; p += 16) {
            t.rect(p, 0, 1, TS, C(R.BROWN, 1));
            t.rect(p + 1, 0, 1, TS, C(R.BROWN, 9));
        }
        return t;
    }

    function texBrick(rng) {
        var t = new Raster(TS, TS, C(R.BROWN, 5));
        var bh = 8;
        for (var by = 0; by < TS; by += bh) {
            var off = ((by / bh) & 1) ? 8 : 0;
            for (var bx = 0; bx < TS; bx += 16) {
                var x = (bx + off) % TS;
                var s = 4 + ((rng() * 4) | 0);
                for (var yy = 0; yy < bh - 1; yy++) {
                    for (var xx = 0; xx < 15; xx++) {
                        t.set((x + xx) % TS, by + yy, C(R.BROWN, s + (yy === 0 ? 2 : (yy === bh - 2 ? -2 : 0))));
                    }
                }
            }
            t.rect(0, by + bh - 1, TS, 1, C(R.GREY, 3));
        }
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function texSwitch(rng) {
        var t = texMetal(rng);
        t.rect(20, 18, 24, 28, C(R.STEEL, 2));
        t.frame(20, 18, 24, 28, C(R.STEEL, 9), 2);
        t.rect(25, 23, 14, 8, C(R.RED, 11));      // "off" lamp, lit red
        t.rect(25, 33, 14, 8, C(R.STEEL, 4));
        t.frame(25, 23, 14, 8, C(R.STEEL, 1), 1);
        t.frame(25, 33, 14, 8, C(R.STEEL, 1), 1);
        return t;
    }

    function texSupport(rng) {
        var t = new Raster(TS, TS, C(R.STEEL, 4));
        t.hgrad(0, 0, TS, TS, R.STEEL, 2, 8);
        t.hgrad(TS / 2, 0, TS / 2, TS, R.STEEL, 8, 2);
        t.rect(0, 0, 6, TS, C(R.BROWN, 5));
        t.rect(TS - 6, 0, 6, TS, C(R.BROWN, 5));
        for (var y = 0; y < TS; y += 8) {
            t.rect(0, y, TS, 1, C(R.STEEL, 1));
            t.circle(3, y + 4, 1.5, C(R.BROWN, 9));
            t.circle(TS - 4, y + 4, 1.5, C(R.BROWN, 9));
        }
        return t;
    }

    function texGStone(rng) {
        var t = texStone(rng);
        var n = fbm(TS, 4, 3, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) {
                if (n(x, y) > 0.5) {
                    var c = t.px[y * TS + x];
                    t.setFast(x, y, C(R.OOZE, Math.max(1, (c & 15) - 3)));
                }
            }
        }
        return t;
    }

    function texSkullWall(rng) {
        var t = texMarble(rng);
        // rows of embedded skulls
        for (var sy = 8; sy < TS; sy += 24) {
            for (var sx = 8; sx < TS; sx += 22) {
                var cx = sx + 6, cy = sy + 8;
                t.ellipse(cx, cy, 7, 8, C(R.BONE, 11));
                t.ellipse(cx, cy + 7, 5, 4, C(R.BONE, 9));
                t.ellipse(cx - 3, cy - 1, 2.2, 2.6, C(R.BLOOD, 0));
                t.ellipse(cx + 3, cy - 1, 2.2, 2.6, C(R.BLOOD, 0));
                t.rect(cx - 1, cy + 3, 2, 3, C(R.BLOOD, 0));
                for (var i = -3; i <= 3; i += 2) t.rect(cx + i, cy + 8, 1, 3, C(R.BLOOD, 1));
            }
        }
        return t;
    }

    function texBars(rng) {
        // Window / cage texture: rendered with see-through gaps by the caster.
        var t = new Raster(TS, TS, C(R.STEEL, 3));
        t.rect(0, 0, TS, TS, C(R.STEEL, 2));
        for (var x = 4; x < TS; x += 12) t.rect(x, 0, 5, TS, C(R.STEEL, 8));
        t.rect(0, 0, TS, 6, C(R.STEEL, 6));
        t.rect(0, TS - 6, TS, 6, C(R.STEEL, 6));
        t.speckle(0, 0, TS, TS, rng, 2);
        return t;
    }

    // ------------------------------------------------------------------ flats

    function flatFloor(rng) {
        var t = new Raster(TS, TS, C(R.GREY, 5));
        var n = fbm(TS, 6, 3, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) t.setFast(x, y, C(R.GREY, 3 + Math.round(n(x, y) * 6)));
        }
        for (var i = 0; i < TS; i += 32) {
            t.rect(i, 0, 1, TS, C(R.GREY, 2));
            t.rect(0, i, TS, 1, C(R.GREY, 2));
        }
        return t;
    }

    function flatTech(rng) {
        var t = new Raster(TS, TS, C(R.STEEL, 4));
        for (var q = 0; q < 4; q++) {
            var x = (q & 1) * 32, y = (q >> 1) * 32;
            t.rect(x + 1, y + 1, 30, 30, C(R.STEEL, 4));
            t.frame(x + 1, y + 1, 30, 30, C(R.STEEL, 7), 1);
            t.circle(x + 16, y + 16, 5, C(R.STEEL, 2));
            t.circle(x + 16, y + 16, 3, C(R.STEEL, 6));
        }
        t.speckle(0, 0, TS, TS, rng, 1);
        return t;
    }

    function flatOoze(rng, ramp, frames) {
        // Animated turbulent slime -- the noise field is scrolled per frame.
        var out = [];
        var n = fbm(TS, 4, 3, rng);
        for (var f = 0; f < frames; f++) {
            var t = new Raster(TS, TS, C(ramp, 6));
            var ph = (f / frames) * U.PI2;
            for (var y = 0; y < TS; y++) {
                for (var x = 0; x < TS; x++) {
                    var wx = x + Math.sin(y * 0.19 + ph) * 3;
                    var wy = y + Math.cos(x * 0.17 + ph) * 3;
                    var v = n(wx, wy);
                    t.setFast(x, y, C(ramp, 2 + Math.round(v * 11)));
                }
            }
            out.push(t);
        }
        return out;
    }

    function flatGrate(rng) {
        var t = new Raster(TS, TS, C(R.STEEL, 1));
        for (var y = 0; y < TS; y += 8) {
            for (var x = 0; x < TS; x += 8) {
                t.rect(x, y, 7, 7, C(R.STEEL, 5));
                t.rect(x + 2, y + 2, 3, 3, C(R.STEEL, 0));
                t.rect(x, y, 7, 1, C(R.STEEL, 8));
            }
        }
        return t;
    }

    function flatCeil(rng, bright) {
        var t = new Raster(TS, TS, C(R.GREY, bright ? 8 : 3));
        var n = fbm(TS, 5, 2, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) {
                t.setFast(x, y, C(R.GREY, (bright ? 6 : 1) + Math.round(n(x, y) * 5)));
            }
        }
        if (bright) {
            // recessed light panel
            t.rect(12, 12, 40, 40, C(R.YELLOW, 13));
            t.frame(12, 12, 40, 40, C(R.GREY, 9), 3);
            t.rect(16, 16, 32, 32, C(R.YELLOW, 15));
        }
        return t;
    }

    function flatPentagram(rng) {
        var t = new Raster(TS, TS, C(R.BLOOD, 3));
        var n = fbm(TS, 5, 3, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) t.setFast(x, y, C(R.BLOOD, 2 + Math.round(n(x, y) * 5)));
        }
        var cx = 32, cy = 32, rad = 27;
        // inscribed circle
        for (var a = 0; a < 360; a++) {
            var ra = a * Math.PI / 180;
            t.circle(cx + Math.cos(ra) * rad, cy + Math.sin(ra) * rad, 1.2, C(R.FIRE, 13));
        }
        // five-pointed star: connect every second vertex
        var pts = [];
        for (var i = 0; i < 5; i++) {
            var ang = -Math.PI / 2 + i * U.PI2 / 5;
            pts.push([cx + Math.cos(ang) * rad, cy + Math.sin(ang) * rad]);
        }
        for (var j = 0; j < 5; j++) {
            var p = pts[j], q = pts[(j + 2) % 5];
            t.line(p[0], p[1], q[0], q[1], C(R.FIRE, 14));
            t.line(p[0], p[1] + 1, q[0], q[1] + 1, C(R.FIRE, 11));
        }
        return t;
    }

    function flatHellRock(rng) {
        var t = new Raster(TS, TS, C(R.BROWN, 4));
        var n = fbm(TS, 4, 4, rng);
        for (var y = 0; y < TS; y++) {
            for (var x = 0; x < TS; x++) {
                var v = n(x, y);
                t.setFast(x, y, v > 0.62 ? C(R.BLOOD, 2 + Math.round(v * 5))
                    : C(R.BROWN, 2 + Math.round(v * 7)));
            }
        }
        return t;
    }

    // -------------------------------------------------------------------- sky

    var SKY_W = 256, SKY_H = 128;

    function makeSky(rng, hellish) {
        var t = new Raster(SKY_W, SKY_H, 0);
        var topRamp = hellish ? R.BLOOD : R.BLUE;
        var botRamp = hellish ? R.FIRE : R.STEEL;
        for (var y = 0; y < SKY_H; y++) {
            var f = y / (SKY_H - 1);
            var ramp = f < 0.55 ? topRamp : botRamp;
            var s = f < 0.55 ? U.lerp(2, 7, f / 0.55) : U.lerp(4, 9, (f - 0.55) / 0.45);
            t.rect(0, y, SKY_W, 1, C(ramp, Math.round(s)));
        }
        // Distant mountain silhouette -- Doom's E1 sky in one line of noise.
        var ridge = fbm(SKY_W, 5, 3, rng);
        for (var x = 0; x < SKY_W; x++) {
            var h = SKY_H * (0.58 + ridge(x, 8) * 0.26);
            for (var yy = h | 0; yy < SKY_H; yy++) {
                var d = (yy - h) / (SKY_H - h);
                t.setFast(x, yy, C(hellish ? R.BLOOD : R.GREY, Math.round(U.lerp(4, 0, d))));
            }
        }
        if (hellish) {
            for (var i = 0; i < 90; i++) {
                t.circle(rng() * SKY_W, rng() * SKY_H * 0.5, rng() * 1.5, C(R.FIRE, 9 + ((rng() * 6) | 0)));
            }
        } else {
            for (var k = 0; k < 60; k++) {
                t.set((rng() * SKY_W) | 0, (rng() * SKY_H * 0.45) | 0, C(R.BONE, 12));
            }
        }
        return t;
    }

    // ------------------------------------------------------------------- build

    var built = null;

    /** Build (once) and return every texture, flat and sky the game needs. */
    function build() {
        if (built) return built;
        var rng = U.makeRng(0x1D00D00 ^ 0x9E3779B9);  // fixed seed: stable art

        var walls = [];
        walls[TEX.STONE] = texStone(rng);
        walls[TEX.TECH] = texTech(rng);
        walls[TEX.METAL] = texMetal(rng);
        walls[TEX.COMPUTER] = texComputer(rng);
        walls[TEX.NUKEWALL] = texNukeWall(rng);
        walls[TEX.EXITSIGN] = texExitSign(rng);
        walls[TEX.DOOR] = texDoor(rng);
        walls[TEX.DOORTRAK] = texDoorTrack(rng);
        walls[TEX.MARBLE] = texMarble(rng);
        walls[TEX.SKIN] = texSkin(rng);
        walls[TEX.WOOD] = texWood(rng);
        walls[TEX.BRICK] = texBrick(rng);
        walls[TEX.DOOR_RED] = texDoor(rng, R.RED);
        walls[TEX.DOOR_BLUE] = texDoor(rng, R.BLUE);
        walls[TEX.DOOR_YELLOW] = texDoor(rng, R.YELLOW);
        walls[TEX.SWITCH] = texSwitch(rng);
        walls[TEX.SUPPORT] = texSupport(rng);
        walls[TEX.GSTONE] = texGStone(rng);
        walls[TEX.SKULLWALL] = texSkullWall(rng);
        walls[TEX.BARS] = texBars(rng);

        var flats = [];
        flats[FLAT.FLOOR] = [flatFloor(rng)];
        flats[FLAT.TECHFLOOR] = [flatTech(rng)];
        flats[FLAT.NUKAGE] = flatOoze(rng, R.OOZE, 4);
        flats[FLAT.BLOOD] = flatOoze(rng, R.BLOOD, 4);
        flats[FLAT.GRATE] = [flatGrate(rng)];
        flats[FLAT.CEIL] = [flatCeil(rng, false)];
        flats[FLAT.CEILLIGHT] = [flatCeil(rng, true)];
        flats[FLAT.PENTAGRAM] = [flatPentagram(rng)];
        flats[FLAT.HELLROCK] = [flatHellRock(rng)];
        flats[FLAT.HELLCEIL] = [flatOoze(rng, R.BLOOD, 1)[0]];

        built = {
            size: TS,
            walls: walls,
            flats: flats,
            skies: { tech: makeSky(rng, false), hell: makeSky(rng, true) },
            skyW: SKY_W,
            skyH: SKY_H
        };
        return built;
    }

    DOOM.TEX = TEX;
    DOOM.FLAT = FLAT;
    DOOM.SKY_FLAT = SKY_FLAT;
    DOOM.TEX_SIZE = TS;
    DOOM.Textures = { build: build };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 03_sprites.js --- */

/* ==========================================================================
 * DOOM :: sprites.js -- procedurally drawn monsters, pickups and effects
 *
 * Doom sprites were 8-rotation billboards. We draw three base views
 * (front / three-quarter / side / back) and mirror them for the other half of
 * the compass, which is exactly how the original saved WAD space too.
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;
    var C = U.C, R = U.RAMP, T = U.TRANSPARENT;
    var Raster = DOOM.Raster;

    // --------------------------------------------------------------- helpers

    /** Thick line, drawn as a swept disc -- our substitute for a limb. */
    function limb(r, x0, y0, x1, y1, w, c) {
        var steps = Math.max(2, Math.ceil(Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0))));
        for (var i = 0; i <= steps; i++) {
            var t = i / steps;
            r.circle(U.lerp(x0, x1, t), U.lerp(y0, y1, t), w / 2, c);
        }
    }

    /** Cheap volume shading: darken the left third, brighten a highlight edge. */
    function shadeVolume(r, x, y, w, h, lightFromLeft) {
        var third = Math.max(1, Math.round(w / 3));
        if (lightFromLeft) {
            r.adjust(x, y, third, h, 2);
            r.adjust(x + w - third, y, third, h, -2);
        } else {
            r.adjust(x, y, third, h, -2);
            r.adjust(x + w - third, y, third, h, 2);
        }
    }

    /** Trim a sprite's fully transparent border so billboards scale tightly. */
    function trim(r) {
        var minX = r.w, minY = r.h, maxX = -1, maxY = -1;
        for (var y = 0; y < r.h; y++) {
            for (var x = 0; x < r.w; x++) {
                if (r.px[y * r.w + x] !== T) {
                    if (x < minX) minX = x;
                    if (x > maxX) maxX = x;
                    if (y < minY) minY = y;
                    if (y > maxY) maxY = y;
                }
            }
        }
        if (maxX < 0) return { raster: r, ax: 0.5, ay: 1 };
        var nw = maxX - minX + 1, nh = maxY - minY + 1;
        var out = new Raster(nw, nh, T);
        for (var yy = 0; yy < nh; yy++) {
            for (var xx = 0; xx < nw; xx++) {
                out.px[yy * nw + xx] = r.px[(yy + minY) * r.w + (xx + minX)];
            }
        }
        return {
            raster: out,
            // anchor of the original canvas' bottom-centre inside the trimmed art
            ax: (r.w / 2 - minX) / nw,
            ay: (r.h - minY) / nh
        };
    }

    function finish(r) {
        var t = trim(r);
        return { w: t.raster.w, h: t.raster.h, px: t.raster.px, ax: t.ax, ay: t.ay };
    }

    // ------------------------------------------------------------- humanoids

    var HUM_W = 46, HUM_H = 62;

    /**
     * The shared skeleton behind Zombieman, Shotgun Guy and (loosely) the
     * Baron. `o` selects palette, proportions, weapon and pose.
     */
    function drawHumanoid(o) {
        var r = new Raster(HUM_W, HUM_H, T);
        var cx = HUM_W / 2 + (o.offsetX || 0);
        var ground = HUM_H - 2;
        var view = o.view;                       // 0 front, 1 three-quarter, 2 side, 3 back
        var ph = o.phase || 0;                   // walk phase 0..3
        var swing = [0, 1, 0, -1][ph & 3];
        var bob = (ph === 1 || ph === 3) ? 1 : 0;
        var suit = o.suit, skin = o.skin, boot = o.boot === undefined ? R.BROWN : o.boot;
        var scale = o.scale || 1;

        var hipY = ground - Math.round(22 * scale) + bob;
        var shoulderY = hipY - Math.round(20 * scale);
        var headR = Math.round(6 * scale);
        var headY = shoulderY - headR - 1;
        var torsoW = Math.round((view === 2 ? 12 : 19) * scale);

        // ---- legs
        var legSpread = Math.round(5 * scale);
        var stride = Math.round(swing * 5 * scale);
        limb(r, cx - legSpread, hipY, cx - legSpread + stride, ground - 3, 7 * scale, C(suit, 4));
        limb(r, cx + legSpread, hipY, cx + legSpread - stride, ground - 3, 7 * scale, C(suit, 5));
        r.ellipse(cx - legSpread + stride, ground - 2, 5 * scale, 3 * scale, C(boot, 3));
        r.ellipse(cx + legSpread - stride, ground - 2, 5 * scale, 3 * scale, C(boot, 4));

        // ---- torso
        r.ellipse(cx, (hipY + shoulderY) / 2, torsoW / 2, (hipY - shoulderY) / 2 + 2, C(suit, 7));
        shadeVolume(r, cx - torsoW / 2, shoulderY, torsoW, hipY - shoulderY, view !== 3);
        if (o.vest) {
            r.rect(cx - torsoW / 2 + 2, shoulderY + 4, torsoW - 4, Math.round(9 * scale), C(o.vest, 6));
            r.rect(cx - torsoW / 2 + 2, shoulderY + 4, torsoW - 4, 1, C(o.vest, 10));
        }
        if (o.beltRamp !== undefined) r.rect(cx - torsoW / 2, hipY - 3, torsoW, 3, C(o.beltRamp, 4));

        // ---- head
        r.circle(cx + (view === 2 ? 1 : 0), headY, headR, C(skin, 8));
        r.ellipse(cx, headY + headR - 1, headR * 0.9, 2, C(skin, 6));
        if (o.helmet) {
            r.ellipse(cx, headY - headR * 0.35, headR + 1, headR * 0.8, C(suit, 5));
            r.ellipse(cx, headY - headR * 0.9, headR + 1, headR * 0.35, C(suit, 8));
        }
        if (view !== 3) {
            // eyes: undead sclera-less black with a hot pinpoint
            var ex = headR * 0.45, ey = headY - 1;
            if (view === 2) {
                r.ellipse(cx + 3, ey, 1.6, 2, C(R.BLOOD, 0));
                r.set(cx + 3, ey - 1, C(R.YELLOW, 13));
            } else {
                r.ellipse(cx - ex, ey, 1.6, 2.2, C(R.BLOOD, 0));
                r.ellipse(cx + ex, ey, 1.6, 2.2, C(R.BLOOD, 0));
                r.set(Math.round(cx - ex), ey - 1, C(R.YELLOW, 13));
                r.set(Math.round(cx + ex), ey - 1, C(R.YELLOW, 13));
            }
            r.rect(cx - 2, headY + headR * 0.45, 4, 1, C(R.BLOOD, 3));
        }

        // ---- arms & weapon
        var armY = shoulderY + Math.round(3 * scale);
        var reach = o.pose === 'attack' ? Math.round(13 * scale) : Math.round(6 * scale);
        var handY = o.pose === 'attack' ? armY + 2 : hipY - Math.round(3 * scale);
        var lax = cx - torsoW / 2 - 1, rax = cx + torsoW / 2 + 1;

        if (o.pose === 'pain') {
            limb(r, lax, armY, lax - 9 * scale, armY - 8 * scale, 5 * scale, C(suit, 6));
            limb(r, rax, armY, rax + 9 * scale, armY - 8 * scale, 5 * scale, C(suit, 6));
        } else if (o.weapon && view !== 3) {
            var hx = cx + (view === 2 ? reach : reach * 0.55);
            limb(r, lax, armY, cx + reach * 0.2, handY, 5 * scale, C(suit, 6));
            limb(r, rax, armY, hx, handY, 5 * scale, C(suit, 7));
            drawHeldWeapon(r, o.weapon, hx, handY, scale, o.pose === 'attack');
        } else {
            limb(r, lax, armY, lax - 3 + stride * 0.6, hipY + 2, 5 * scale, C(suit, 6));
            limb(r, rax, armY, rax + 3 - stride * 0.6, hipY + 2, 5 * scale, C(suit, 7));
        }
        return r;
    }

    function drawHeldWeapon(r, kind, x, y, scale, firing) {
        if (kind === 'pistol') {
            r.rect(x - 1, y - 2, 7 * scale, 3, C(R.STEEL, 3));
            r.rect(x - 1, y, 3, 4, C(R.STEEL, 2));
            if (firing) {
                r.circle(x + 7 * scale, y - 1, 3.2, C(R.FIRE, 15));
                r.circle(x + 9 * scale, y - 1, 1.8, C(R.YELLOW, 15));
            }
        } else if (kind === 'shotgun') {
            r.rect(x - 4, y - 3, 14 * scale, 3, C(R.STEEL, 3));
            r.rect(x - 4, y, 8 * scale, 3, C(R.BROWN, 4));
            if (firing) {
                r.circle(x + 14 * scale, y - 2, 4.5, C(R.FIRE, 15));
                r.circle(x + 17 * scale, y - 2, 2.5, C(R.YELLOW, 15));
                r.circle(x + 12 * scale, y - 5, 2, C(R.FIRE, 12));
            }
        }
    }

    // -------------------------------------------------------------- monsters

    function drawImp(o) {
        // Hunched, dark-brown, clawed, hurls fireballs.
        var r = new Raster(HUM_W, HUM_H, T);
        var cx = HUM_W / 2, ground = HUM_H - 2;
        var ph = o.phase || 0, view = o.view;
        var swing = [0, 1, 0, -1][ph & 3];
        var skin = R.BROWN;
        var hipY = ground - 20, shoulderY = hipY - 17;
        var headY = shoulderY - 6;

        // digitigrade legs
        limb(r, cx - 5, hipY, cx - 7 - swing * 2, hipY + 10, 7, C(skin, 5));
        limb(r, cx - 7 - swing * 2, hipY + 10, cx - 6 + swing * 4, ground - 2, 5, C(skin, 4));
        limb(r, cx + 5, hipY, cx + 7 + swing * 2, hipY + 10, 7, C(skin, 6));
        limb(r, cx + 7 + swing * 2, hipY + 10, cx + 6 - swing * 4, ground - 2, 5, C(skin, 5));
        r.ellipse(cx - 6 + swing * 4, ground - 1, 5, 2.5, C(skin, 3));
        r.ellipse(cx + 6 - swing * 4, ground - 1, 5, 2.5, C(skin, 3));

        // hunched torso
        r.ellipse(cx, (hipY + shoulderY) / 2, view === 2 ? 7 : 11, 11, C(skin, 7));
        r.ellipse(cx, shoulderY + 2, view === 2 ? 8 : 13, 6, C(skin, 8));
        shadeVolume(r, cx - 13, shoulderY, 26, hipY - shoulderY, view !== 3);
        // pale belly
        if (view !== 3) r.ellipse(cx, hipY - 6, 6, 6, C(R.BONE, 6));

        // shoulder spikes -- the imp's unmistakable silhouette
        for (var s = -1; s <= 1; s += 2) {
            r.poly([[cx + s * 9, shoulderY], [cx + s * 14, shoulderY - 8], [cx + s * 11, shoulderY + 2]], C(R.BONE, 9));
        }

        // head with horns
        r.ellipse(cx, headY, 6.5, 6, C(skin, 8));
        r.poly([[cx - 6, headY - 3], [cx - 11, headY - 11], [cx - 4, headY - 6]], C(R.BONE, 10));
        r.poly([[cx + 6, headY - 3], [cx + 11, headY - 11], [cx + 4, headY - 6]], C(R.BONE, 10));
        if (view !== 3) {
            r.ellipse(cx - 2.6, headY - 1, 1.8, 1.6, C(R.YELLOW, 14));
            r.ellipse(cx + 2.6, headY - 1, 1.8, 1.6, C(R.YELLOW, 14));
            // snarl
            r.rect(cx - 4, headY + 3, 8, 2, C(R.BLOOD, 1));
            for (var tx = -3; tx <= 3; tx += 2) r.rect(cx + tx, headY + 3, 1, 2, C(R.BONE, 13));
        }

        // arms -- raised and glowing when throwing
        var armY = shoulderY + 2;
        if (o.pose === 'attack') {
            limb(r, cx - 10, armY, cx - 15, armY - 10, 5, C(skin, 6));
            limb(r, cx + 10, armY, cx + 15, armY - 10, 5, C(skin, 7));
            r.circle(cx + 16, armY - 12, 5, C(R.FIRE, 15));
            r.circle(cx + 16, armY - 12, 3, C(R.YELLOW, 15));
            r.circle(cx - 16, armY - 12, 3.5, C(R.FIRE, 12));
        } else if (o.pose === 'pain') {
            limb(r, cx - 10, armY, cx - 17, armY - 6, 5, C(skin, 6));
            limb(r, cx + 10, armY, cx + 17, armY - 6, 5, C(skin, 7));
        } else {
            limb(r, cx - 10, armY, cx - 12 + swing * 2, hipY + 4, 5, C(skin, 6));
            limb(r, cx + 10, armY, cx + 12 - swing * 2, hipY + 4, 5, C(skin, 7));
            // claws
            for (var k = -1; k <= 1; k += 2) {
                for (var f = -1; f <= 1; f++) {
                    r.line(cx + k * 12, hipY + 6, cx + k * 12 + f * 2, hipY + 10, C(R.BONE, 11));
                }
            }
        }
        return r;
    }

    function drawDemon(o) {
        // Pinky: low, wide, all shoulders and teeth.
        var r = new Raster(HUM_W + 12, HUM_H, T);
        var cx = (HUM_W + 12) / 2, ground = HUM_H - 2;
        var ph = o.phase || 0, view = o.view;
        var swing = [0, 1, 0, -1][ph & 3];
        var skin = R.FLESH;
        var bodyY = ground - 26;

        // four stubby legs
        for (var i = -1; i <= 1; i += 2) {
            limb(r, cx + i * 11, bodyY + 12, cx + i * 13 + swing * 2, ground - 2, 8, C(skin, 5));
            limb(r, cx + i * 5, bodyY + 14, cx + i * 6 - swing * 2, ground - 2, 7, C(skin, 4));
            r.ellipse(cx + i * 13 + swing * 2, ground - 1, 5, 2.5, C(R.BONE, 8));
        }

        // barrel body
        r.ellipse(cx, bodyY + 4, view === 2 ? 12 : 19, 13, C(skin, 7));
        shadeVolume(r, cx - 19, bodyY - 9, 38, 26, view !== 3);
        r.ellipse(cx, bodyY + 10, view === 2 ? 9 : 14, 6, C(R.BONE, 7));   // pale underside

        // head fused to shoulders
        var headY = bodyY - 8 - (o.pose === 'attack' ? 2 : 0);
        r.ellipse(cx, headY, 12, 9, C(skin, 8));
        if (view !== 3) {
            r.ellipse(cx - 5, headY - 3, 2.2, 2, C(R.RED, 13));
            r.ellipse(cx + 5, headY - 3, 2.2, 2, C(R.RED, 13));
            r.ellipse(cx - 8, headY - 6, 3, 5, C(skin, 6));   // brow horns
            r.ellipse(cx + 8, headY - 6, 3, 5, C(skin, 6));
            // the jaw -- wide open on the bite frame
            var gape = o.pose === 'attack' ? 9 : 4;
            r.ellipse(cx, headY + 4, 9, gape, C(R.BLOOD, 1));
            for (var tx = -8; tx <= 8; tx += 3) {
                r.poly([[cx + tx, headY + 4 - gape], [cx + tx + 2, headY + 4 - gape], [cx + tx + 1, headY + 4 - gape + 4]], C(R.BONE, 14));
                r.poly([[cx + tx, headY + 4 + gape], [cx + tx + 2, headY + 4 + gape], [cx + tx + 1, headY + 4 + gape - 4]], C(R.BONE, 14));
            }
        }
        if (o.pose === 'pain') r.adjust(0, 0, r.w, r.h, 2);
        return r;
    }

    function drawBaron(o) {
        // Baron of Hell: pink-grey torso, green trunks, goat legs, huge.
        var r = new Raster(HUM_W + 16, HUM_H + 16, T);
        var cx = (HUM_W + 16) / 2, ground = HUM_H + 16 - 2;
        var ph = o.phase || 0, view = o.view;
        var swing = [0, 1, 0, -1][ph & 3];
        var skin = R.FLESH;
        var hipY = ground - 28, shoulderY = hipY - 24, headY = shoulderY - 10;

        // digitigrade goat legs with hooves
        for (var i = -1; i <= 1; i += 2) {
            limb(r, cx + i * 7, hipY, cx + i * 11 + swing * i * 2, hipY + 13, 10, C(skin, 6));
            limb(r, cx + i * 11 + swing * i * 2, hipY + 13, cx + i * 8 - swing * i * 3, ground - 5, 6, C(skin, 5));
            r.ellipse(cx + i * 8 - swing * i * 3, ground - 3, 6, 4, C(R.GREY, 2));
        }
        r.rect(cx - 13, hipY - 6, 26, 10, C(R.GREEN, 4));       // trunks
        r.rect(cx - 13, hipY - 6, 26, 2, C(R.GREEN, 8));

        // massive torso
        r.ellipse(cx, (hipY + shoulderY) / 2 - 2, view === 2 ? 13 : 20, 16, C(skin, 8));
        r.ellipse(cx, shoulderY + 3, view === 2 ? 14 : 23, 8, C(skin, 9));
        shadeVolume(r, cx - 23, shoulderY - 5, 46, hipY - shoulderY + 10, view !== 3);
        if (view !== 3) {
            for (var ab = 0; ab < 3; ab++) r.rect(cx - 8, hipY - 14 + ab * 4, 16, 1, C(skin, 4));
        }

        // head + big backswept horns
        r.ellipse(cx, headY, 9, 8, C(skin, 9));
        for (var s = -1; s <= 1; s += 2) {
            r.poly([[cx + s * 7, headY - 4], [cx + s * 18, headY - 14], [cx + s * 20, headY - 8], [cx + s * 8, headY + 1]], C(R.BONE, 11));
        }
        if (view !== 3) {
            r.ellipse(cx - 3.5, headY - 1, 2.2, 2, C(R.GREEN, 14));
            r.ellipse(cx + 3.5, headY - 1, 2.2, 2, C(R.GREEN, 14));
            r.ellipse(cx, headY + 5, 6, 3, C(R.BLOOD, 1));
            for (var tx = -4; tx <= 4; tx += 2) r.rect(cx + tx, headY + 3, 1, 3, C(R.BONE, 13));
        }

        // arms; both palms alight on the attack frame
        var armY = shoulderY + 4;
        if (o.pose === 'attack') {
            limb(r, cx - 18, armY, cx - 24, armY - 12, 7, C(skin, 7));
            limb(r, cx + 18, armY, cx + 24, armY - 12, 7, C(skin, 8));
            r.circle(cx - 25, armY - 15, 6, C(R.GREEN, 15));
            r.circle(cx + 25, armY - 15, 6, C(R.GREEN, 15));
            r.circle(cx + 25, armY - 15, 3, C(R.BONE, 15));
        } else if (o.pose === 'pain') {
            limb(r, cx - 18, armY, cx - 26, armY - 8, 7, C(skin, 7));
            limb(r, cx + 18, armY, cx + 26, armY - 8, 7, C(skin, 8));
        } else {
            limb(r, cx - 18, armY, cx - 20 + swing * 3, hipY + 2, 7, C(skin, 7));
            limb(r, cx + 18, armY, cx + 20 - swing * 3, hipY + 2, 7, C(skin, 8));
        }
        return r;
    }

    function drawCyberdemon(o) {
        // Half flesh, half machine, entirely bad news.
        var r = new Raster(HUM_W + 26, HUM_H + 26, T);
        var cx = (HUM_W + 26) / 2, ground = HUM_H + 26 - 2;
        var ph = o.phase || 0, view = o.view;
        var swing = [0, 1, 0, -1][ph & 3];
        var skin = R.BROWN;
        var hipY = ground - 34, shoulderY = hipY - 28, headY = shoulderY - 12;

        // one organic leg, one hydraulic
        limb(r, cx - 8, hipY, cx - 13 + swing * 2, hipY + 16, 12, C(skin, 6));
        limb(r, cx - 13 + swing * 2, hipY + 16, cx - 10 - swing * 3, ground - 6, 7, C(skin, 5));
        r.ellipse(cx - 10 - swing * 3, ground - 4, 7, 4, C(R.GREY, 2));
        limb(r, cx + 8, hipY, cx + 13 - swing * 2, hipY + 16, 11, C(R.STEEL, 5));
        limb(r, cx + 13 - swing * 2, hipY + 16, cx + 10 + swing * 3, ground - 6, 6, C(R.STEEL, 7));
        r.rect(cx + 4, ground - 7, 13, 6, C(R.STEEL, 4));
        r.rect(cx + 4, ground - 7, 13, 1, C(R.STEEL, 10));
        for (var pj = 0; pj < 3; pj++) r.rect(cx + 9 - swing, hipY + 6 + pj * 4, 9, 2, C(R.STEEL, 9));

        // torso, half plated
        r.ellipse(cx, (hipY + shoulderY) / 2, view === 2 ? 15 : 23, 18, C(skin, 7));
        r.rect(cx, shoulderY, 24, hipY - shoulderY, C(R.STEEL, 5));
        r.ellipse(cx + 6, (hipY + shoulderY) / 2, 17, 17, C(R.STEEL, 5));
        for (var pl = 0; pl < 4; pl++) r.rect(cx - 4, shoulderY + 4 + pl * 7, 24, 1, C(R.STEEL, 8));
        r.ellipse(cx - 10, (hipY + shoulderY) / 2, 12, 16, C(skin, 8));
        // exposed ribs on the flesh side
        for (var rb = 0; rb < 4; rb++) r.rect(cx - 20, shoulderY + 8 + rb * 6, 14, 2, C(R.BONE, 9));

        // head: skull-like with a steel jaw
        r.ellipse(cx - 2, headY, 11, 10, C(skin, 8));
        r.ellipse(cx - 2, headY + 6, 9, 4, C(R.STEEL, 7));
        for (var s = -1; s <= 1; s += 2) {
            r.poly([[cx - 2 + s * 9, headY - 5], [cx - 2 + s * 22, headY - 18], [cx - 2 + s * 24, headY - 11], [cx - 2 + s * 10, headY]], C(R.BONE, 12));
        }
        if (view !== 3) {
            r.ellipse(cx - 6, headY - 1, 2.6, 2.4, C(R.RED, 15));
            r.ellipse(cx + 2, headY - 1, 2.6, 2.4, C(R.RED, 15));
            for (var tx2 = -8; tx2 <= 4; tx2 += 3) r.rect(cx + tx2, headY + 4, 2, 4, C(R.STEEL, 12));
        }

        // left arm organic, right arm is the rocket launcher
        var armY = shoulderY + 6;
        limb(r, cx - 20, armY, cx - 26 + swing * 2, hipY + 4, 9, C(skin, 7));
        limb(r, cx + 18, armY, cx + 26, armY + 4, 9, C(R.STEEL, 6));
        r.rect(cx + 24, armY - 4, 22, 13, C(R.STEEL, 4));
        r.frame(cx + 24, armY - 4, 22, 13, C(R.STEEL, 8), 1);
        r.rect(cx + 44, armY - 1, 6, 7, C(R.STEEL, 2));
        r.rect(cx + 26, armY - 8, 16, 5, C(R.STEEL, 6));      // rocket rack
        for (var rk = 0; rk < 3; rk++) r.circle(cx + 29 + rk * 5, armY - 6, 2, C(R.RED, 9));
        if (o.pose === 'attack') {
            r.circle(cx + 50, armY + 2, 7, C(R.FIRE, 15));
            r.circle(cx + 54, armY + 2, 4.5, C(R.YELLOW, 15));
            r.circle(cx + 46, armY - 3, 3, C(R.FIRE, 12));
        }
        if (o.pose === 'pain') r.adjust(0, 0, r.w, r.h, 2);
        return r;
    }

    // ----------------------------------------------------------------- death

    /**
     * Progressive collapse used by every monster: the body squashes towards
     * the floor, tips over, and leaks. Frame 0 is the moment of death,
     * the last frame is the corpse left on the ground.
     */
    function makeDeathFrame(base, step, total, rng, gib) {
        var src = base;
        var t = step / (total - 1);
        var out = new Raster(src.w, src.h, T);
        var ground = src.h - 2;

        if (gib && step >= 1) {
            // Gibbing: scatter chunks of the original body outward.
            var chunks = 26 + step * 8;
            for (var i = 0; i < chunks; i++) {
                var sx = (rng() * src.w) | 0, sy = (rng() * src.h) | 0;
                var c = src.px[sy * src.w + sx];
                if (c === T) continue;
                var spread = t * 18;
                var dx = sx + (rng() * 2 - 1) * spread;
                var dy = ground - (1 - t) * (ground - sy) + (rng() * 2 - 1) * spread * 0.35;
                out.circle(dx, Math.min(ground, dy), 1 + rng() * 2.5, c);
            }
            for (var b = 0; b < 40 * t; b++) {
                out.circle(src.w / 2 + (rng() * 2 - 1) * src.w * 0.45, ground - rng() * 5, 1 + rng() * 2, C(R.BLOOD, 3 + ((rng() * 6) | 0)));
            }
            return out;
        }

        // Squash towards the floor while widening slightly, then flatten.
        var squash = 1 - t * 0.86;
        var widen = 1 + t * 0.55;
        var tip = t * t * 10;
        for (var y = 0; y < src.h; y++) {
            for (var x = 0; x < src.w; x++) {
                var col = src.px[y * src.w + x];
                if (col === T) continue;
                var ny = ground - (ground - y) * squash;
                var nx = src.w / 2 + (x - src.w / 2) * widen + (ground - y) * squash * (tip / 14);
                // corpses darken and redden as they settle
                var ramp = col >> 4, sh = col & 15;
                if (t > 0.45 && ramp !== R.BLOOD && col < 240) sh = Math.max(0, sh - Math.round((t - 0.45) * 5));
                out.circle(nx, ny, 1.15, C(ramp, sh));
            }
        }
        // blood pool grows underneath
        if (t > 0.25) {
            var pw = src.w * 0.42 * (t - 0.25) / 0.75;
            out.ellipse(src.w / 2, ground, pw, pw * 0.28, C(R.BLOOD, 3));
            for (var s = 0; s < 14 * t; s++) {
                out.circle(src.w / 2 + (rng() * 2 - 1) * pw * 1.4, ground - rng() * 3, 1 + rng() * 1.6, C(R.BLOOD, 2 + ((rng() * 4) | 0)));
            }
        }
        return out;
    }

    // -------------------------------------------------------------- monsters

    var VIEWS = 4;   // front, three-quarter, side, back

    function buildMonster(drawFn, opts, rng, gib) {
        var set = { walk: [], attack: [], pain: [], death: [] };
        var v, f;
        for (v = 0; v < VIEWS; v++) {
            set.walk[v] = [];
            for (f = 0; f < 4; f++) {
                set.walk[v].push(finish(drawFn(mix(opts, { view: v, phase: f, pose: 'walk' }))));
            }
            set.attack[v] = [];
            for (f = 0; f < 2; f++) {
                set.attack[v].push(finish(drawFn(mix(opts, { view: v, phase: f * 2, pose: f === 0 ? 'walk' : 'attack' }))));
            }
            set.pain[v] = finish(drawFn(mix(opts, { view: v, phase: 0, pose: 'pain' })));
        }
        // Death is view independent, matching the original sprite sets.
        var base = drawFn(mix(opts, { view: 0, phase: 0, pose: 'pain' }));
        var frames = 6;
        for (f = 0; f < frames; f++) set.death.push(finish(makeDeathFrame(base, f, frames, rng, false)));
        if (gib) {
            set.gib = [];
            for (f = 0; f < frames; f++) set.gib.push(finish(makeDeathFrame(base, f, frames, rng, true)));
        }
        return set;
    }

    function mix(a, b) {
        var o = {}, k;
        for (k in a) if (Object.prototype.hasOwnProperty.call(a, k)) o[k] = a[k];
        for (k in b) if (Object.prototype.hasOwnProperty.call(b, k)) o[k] = b[k];
        return o;
    }
    // --------------------------------------------------------------- pickups

    function pickupBase(w, h) { return new Raster(w, h, T); }

    function itemMedikit() {
        var r = pickupBase(28, 22);
        r.rect(1, 2, 26, 19, C(R.BONE, 12));
        r.frame(1, 2, 26, 19, C(R.GREY, 6), 1);
        r.rect(2, 3, 24, 3, C(R.BONE, 15));
        r.rect(11, 6, 6, 12, C(R.RED, 12));
        r.rect(6, 9, 16, 6, C(R.RED, 12));
        r.rect(11, 6, 6, 1, C(R.RED, 15));
        r.rect(9, 1, 10, 3, C(R.GREY, 4));      // handle
        return finish(r);
    }

    function itemStimpack() {
        var r = pickupBase(20, 14);
        r.rect(1, 4, 18, 9, C(R.BONE, 11));
        r.frame(1, 4, 18, 9, C(R.GREY, 5), 1);
        r.rect(3, 6, 9, 5, C(R.PLASMA, 10));
        r.rect(14, 1, 3, 6, C(R.STEEL, 8));     // plunger
        r.rect(12, 6, 6, 5, C(R.RED, 11));
        return finish(r);
    }

    function itemArmor(blue) {
        var ramp = blue ? R.BLUE : R.GREEN;
        var r = pickupBase(26, 26);
        r.poly([[13, 0], [25, 5], [25, 15], [13, 25], [1, 15], [1, 5]], C(ramp, 8));
        r.poly([[13, 3], [22, 7], [22, 15], [13, 22], [4, 15], [4, 7]], C(ramp, 11));
        r.poly([[13, 3], [22, 7], [13, 12], [4, 7]], C(ramp, 14));
        r.rect(12, 8, 3, 10, C(ramp, 5));
        return finish(r);
    }

    function itemSoulsphere() {
        var r = pickupBase(28, 28);
        r.circle(14, 14, 13, C(R.BLUE, 6));
        r.circle(14, 14, 11, C(R.BLUE, 9));
        r.circle(14, 14, 8, C(R.PLASMA, 12));
        r.circle(11, 11, 4, C(R.BONE, 15));
        // the little face inside
        r.ellipse(11, 13, 1.5, 2, C(R.BLUE, 2));
        r.ellipse(17, 13, 1.5, 2, C(R.BLUE, 2));
        r.ellipse(14, 18, 4, 2, C(R.BLUE, 3));
        return finish(r);
    }

    function itemClip() {
        var r = pickupBase(20, 12);
        r.rect(1, 3, 18, 8, C(R.BROWN, 5));
        r.frame(1, 3, 18, 8, C(R.BROWN, 2), 1);
        for (var i = 0; i < 4; i++) {
            r.rect(3 + i * 4, 0, 3, 4, C(R.YELLOW, 10));
            r.rect(3 + i * 4, 0, 1, 4, C(R.YELLOW, 14));
        }
        return finish(r);
    }

    function itemAmmoBox() {
        var r = pickupBase(30, 20);
        r.rect(1, 4, 28, 15, C(R.BROWN, 4));
        r.frame(1, 4, 28, 15, C(R.BROWN, 1), 1);
        r.rect(1, 4, 28, 3, C(R.BROWN, 7));
        r.rect(6, 9, 18, 6, C(R.YELLOW, 9));
        r.rect(6, 9, 18, 1, C(R.YELLOW, 13));
        r.rect(11, 1, 8, 4, C(R.GREY, 4));
        return finish(r);
    }

    function itemShells(box) {
        var r = pickupBase(box ? 30 : 20, box ? 18 : 12);
        var n = box ? 6 : 3;
        if (box) {
            r.rect(1, 3, 28, 14, C(R.BROWN, 4));
            r.frame(1, 3, 28, 14, C(R.BROWN, 1), 1);
        }
        for (var i = 0; i < n; i++) {
            var x = (box ? 3 : 1) + i * (box ? 4.5 : 6);
            var y = box ? 5 : 2;
            r.rect(x, y, box ? 3 : 5, box ? 10 : 9, C(R.RED, 10));
            r.rect(x, y + (box ? 7 : 6), box ? 3 : 5, box ? 3 : 3, C(R.YELLOW, 11));
        }
        return finish(r);
    }

    function itemCell(big) {
        var w = big ? 30 : 18, h = big ? 24 : 16;
        var r = pickupBase(w, h);
        r.rect(1, 1, w - 2, h - 2, C(R.STEEL, 4));
        r.frame(1, 1, w - 2, h - 2, C(R.STEEL, 8), 1);
        var bars = big ? 3 : 2;
        for (var i = 0; i < bars; i++) {
            r.rect(4, 4 + i * ((h - 8) / bars), w - 8, (h - 10) / bars, C(R.PLASMA, 13));
        }
        r.rect(w / 2 - 2, 0, 4, 2, C(R.STEEL, 10));
        return finish(r);
    }

    function itemKey(ramp) {
        var r = pickupBase(16, 22);
        r.rect(3, 2, 10, 14, C(ramp, 11));
        r.frame(3, 2, 10, 14, C(ramp, 5), 1);
        r.rect(5, 4, 6, 3, C(ramp, 14));
        r.rect(6, 16, 4, 6, C(ramp, 8));
        r.rect(6, 19, 7, 2, C(ramp, 8));
        return finish(r);
    }

    function itemWeaponPickup(kind) {
        var r = pickupBase(44, 20);
        if (kind === 'shotgun') {
            r.rect(2, 6, 34, 4, C(R.STEEL, 4));
            r.rect(2, 10, 30, 3, C(R.STEEL, 2));
            r.rect(28, 8, 14, 7, C(R.BROWN, 5));
            r.rect(28, 8, 14, 1, C(R.BROWN, 9));
            r.rect(14, 12, 10, 4, C(R.BROWN, 4));
        } else if (kind === 'chaingun') {
            r.rect(4, 5, 26, 11, C(R.STEEL, 3));
            for (var i = 0; i < 4; i++) r.rect(0, 6 + i * 3, 12, 2, C(R.STEEL, 8));
            r.circle(30, 10, 6, C(R.STEEL, 5));
            r.circle(30, 10, 3, C(R.STEEL, 2));
            r.rect(32, 12, 10, 6, C(R.BROWN, 4));
        } else {
            r.rect(6, 4, 28, 12, C(R.STEEL, 5));
            r.frame(6, 4, 28, 12, C(R.STEEL, 8), 1);
            r.rect(0, 8, 8, 5, C(R.STEEL, 3));
            r.rect(12, 7, 16, 6, C(R.PLASMA, 13));
            r.rect(30, 6, 10, 8, C(R.BROWN, 4));
        }
        return finish(r);
    }

    function itemBarrel(rng) {
        var r = pickupBase(26, 34);
        r.rect(2, 2, 22, 30, C(R.OOZE, 5));
        r.hgrad(2, 2, 22, 30, R.OOZE, 3, 8);
        r.ellipse(13, 3, 11, 3, C(R.OOZE, 9));
        r.ellipse(13, 31, 11, 3, C(R.OOZE, 3));
        r.rect(2, 8, 22, 2, C(R.OOZE, 2));
        r.rect(2, 24, 22, 2, C(R.OOZE, 2));
        // radiation trefoil
        r.circle(13, 17, 3, C(R.YELLOW, 12));
        for (var i = 0; i < 3; i++) {
            var a = -Math.PI / 2 + i * U.PI2 / 3;
            r.poly([
                [13 + Math.cos(a - 0.4) * 9, 17 + Math.sin(a - 0.4) * 9],
                [13 + Math.cos(a + 0.4) * 9, 17 + Math.sin(a + 0.4) * 9],
                [13 + Math.cos(a) * 4, 17 + Math.sin(a) * 4]
            ], C(R.YELLOW, 12));
        }
        r.speckle(2, 2, 22, 30, rng, 1);
        return finish(r);
    }

    function itemLamp() {
        var r = pickupBase(18, 44);
        r.rect(6, 12, 6, 30, C(R.STEEL, 4));
        r.ellipse(9, 42, 8, 3, C(R.STEEL, 3));
        r.ellipse(9, 8, 8, 9, C(R.YELLOW, 14));
        r.ellipse(9, 7, 5, 6, C(R.BONE, 15));
        return finish(r);
    }

    function itemGoreTree() {
        var r = pickupBase(24, 50);
        r.rect(10, 6, 4, 44, C(R.BONE, 8));
        r.ellipse(12, 12, 7, 8, C(R.FLESH, 6));
        r.ellipse(9, 10, 2, 2.4, C(R.BLOOD, 0));
        r.ellipse(15, 10, 2, 2.4, C(R.BLOOD, 0));
        r.ellipse(12, 16, 4, 2, C(R.BLOOD, 1));
        r.ellipse(12, 26, 6, 7, C(R.FLESH, 5));
        r.ellipse(12, 38, 5, 6, C(R.FLESH, 4));
        for (var i = 0; i < 8; i++) r.circle(12 + (i % 3 - 1) * 5, 44 + (i % 2) * 3, 2, C(R.BLOOD, 3));
        return finish(r);
    }

    // ----------------------------------------------------- missiles & effects

    function missile(ramp, size) {
        var out = [];
        for (var f = 0; f < 2; f++) {
            var r = new Raster(size, size, T);
            var c = size / 2;
            var wob = f === 0 ? 1 : 0.85;
            r.circle(c, c, size * 0.44 * wob, C(ramp, 7));
            r.circle(c, c, size * 0.32 * wob, C(ramp, 12));
            r.circle(c, c, size * 0.18, C(R.BONE, 15));
            // flickering tongues of flame
            for (var i = 0; i < 7; i++) {
                var a = (i / 7) * U.PI2 + f * 0.5;
                r.circle(c + Math.cos(a) * size * 0.42, c + Math.sin(a) * size * 0.42, size * 0.09, C(ramp, 10));
            }
            out.push(finish(r));
        }
        return out;
    }

    function rocketMissile() {
        var out = [];
        for (var f = 0; f < 2; f++) {
            var r = new Raster(20, 20, T);
            r.ellipse(10, 10, 7, 4, C(R.STEEL, 6));
            r.poly([[16, 10], [7, 6], [7, 14]], C(R.RED, 9));
            r.circle(3 - f, 10, 4 - f, C(R.FIRE, 15));
            r.circle(1, 10, 2.5, C(R.YELLOW, 14));
            out.push(finish(r));
        }
        return out;
    }

    function explosionFrames(rng) {
        var out = [];
        var n = 6;
        for (var f = 0; f < n; f++) {
            var size = 26 + f * 12;
            var r = new Raster(size, size, T);
            var c = size / 2;
            var t = f / (n - 1);
            var rad = c * (0.35 + t * 0.62);
            for (var i = 0; i < 40; i++) {
                var a = rng() * U.PI2, d = Math.pow(rng(), 0.5) * rad;
                var s = Math.round(U.lerp(15, 3, t) - d / rad * 4);
                r.circle(c + Math.cos(a) * d, c + Math.sin(a) * d,
                    rad * 0.24 * (1 - t * 0.4), C(t > 0.6 ? R.GREY : R.FIRE, s));
            }
            if (t < 0.5) r.circle(c, c, rad * 0.4, C(R.YELLOW, 15));
            out.push(finish(r));
        }
        return out;
    }

    function puffFrames(rng, ramp) {
        var out = [];
        for (var f = 0; f < 4; f++) {
            var size = 12 + f * 4;
            var r = new Raster(size, size, T);
            var c = size / 2;
            for (var i = 0; i < 10; i++) {
                var a = rng() * U.PI2, d = rng() * c * (0.3 + f * 0.22);
                r.circle(c + Math.cos(a) * d, c + Math.sin(a) * d, 2.2 - f * 0.3,
                    C(ramp, Math.max(1, 12 - f * 3)));
            }
            out.push(finish(r));
        }
        return out;
    }

    // ------------------------------------------------------------------ build

    var builtSprites = null;

    function build() {
        if (builtSprites) return builtSprites;
        var rng = U.makeRng(0xD00DAD);

        var monsters = {
            zombieman: buildMonster(drawHumanoid, {
                suit: R.OLIVE, skin: R.FLESH, boot: R.BROWN,
                weapon: 'pistol', vest: R.BROWN, beltRamp: R.BROWN, scale: 1
            }, rng, true),
            sergeant: buildMonster(drawHumanoid, {
                suit: R.BLOOD, skin: R.FLESH, boot: R.GREY,
                weapon: 'shotgun', vest: R.GREY, beltRamp: R.GREY,
                helmet: true, scale: 1.04
            }, rng, true),
            imp: buildMonster(drawImp, {}, rng, true),
            demon: buildMonster(drawDemon, {}, rng, true),
            baron: buildMonster(drawBaron, {}, rng, false),
            cyberdemon: buildMonster(drawCyberdemon, {}, rng, false)
        };

        var items = {
            medikit: itemMedikit(),
            stimpack: itemStimpack(),
            armor: itemArmor(false),
            megaarmor: itemArmor(true),
            soulsphere: itemSoulsphere(),
            clip: itemClip(),
            ammobox: itemAmmoBox(),
            shells: itemShells(false),
            shellbox: itemShells(true),
            cell: itemCell(false),
            cellpack: itemCell(true),
            redkey: itemKey(R.RED),
            bluekey: itemKey(R.BLUE),
            yellowkey: itemKey(R.YELLOW),
            shotgun: itemWeaponPickup('shotgun'),
            chaingun: itemWeaponPickup('chaingun'),
            plasma: itemWeaponPickup('plasma'),
            barrel: itemBarrel(rng),
            lamp: itemLamp(),
            gore: itemGoreTree()
        };

        builtSprites = {
            monsters: monsters,
            items: items,
            missiles: {
                fireball: missile(R.FIRE, 22),
                plasma: missile(R.PLASMA, 18),
                baronball: missile(R.GREEN, 24),
                rocket: rocketMissile()
            },
            fx: {
                explosion: explosionFrames(rng),
                blood: puffFrames(rng, R.BLOOD),
                puff: puffFrames(rng, R.GREY),
                sparks: puffFrames(rng, R.PLASMA)
            }
        };
        return builtSprites;
    }

    DOOM.Sprites = { build: build, VIEWS: VIEWS, trim: trim };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 04_audio.js --- */

/* ==========================================================================
 * DOOM :: audio.js -- 100% synthesised sound effects (Web Audio API)
 *
 * No samples, no fetches. Every gunshot, groan and door is built from
 * oscillators, filtered noise and envelopes at play time.
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;

    var Sound = {
        ctx: null,
        master: null,
        sfxBus: null,
        musicBus: null,
        muted: false,
        musicMuted: false,
        _noise: null,
        _ready: false
    };

    Sound.init = function () {
        if (this.ctx) return this.ctx;
        var Ctx = (typeof window !== 'undefined') && (window.AudioContext || window.webkitAudioContext);
        if (!Ctx) return null;
        try {
            this.ctx = new Ctx();
        } catch (e) {
            return null;
        }
        this.master = this.ctx.createGain();
        this.master.gain.value = 0.85;
        this.master.connect(this.ctx.destination);

        this.sfxBus = this.ctx.createGain();
        this.sfxBus.gain.value = 0.9;
        this.sfxBus.connect(this.master);

        // A gentle bus compressor keeps a chaingun + music from clipping.
        var comp = this.ctx.createDynamicsCompressor();
        comp.threshold.value = -14;
        comp.knee.value = 22;
        comp.ratio.value = 7;
        comp.attack.value = 0.003;
        comp.release.value = 0.2;
        comp.connect(this.master);

        this.musicBus = this.ctx.createGain();
        this.musicBus.gain.value = 0.36;
        this.musicBus.connect(comp);

        this._ready = true;
        return this.ctx;
    };

    Sound.resume = function () {
        if (this.ctx && this.ctx.state === 'suspended') {
            try { this.ctx.resume(); } catch (e) { /* ignore */ }
        }
    };

    Sound.setMuted = function (m) {
        this.muted = !!m;
        if (this.master) this.master.gain.value = this.muted ? 0 : 0.85;
    };

    Sound.setMusicMuted = function (m) {
        this.musicMuted = !!m;
        if (this.musicBus) this.musicBus.gain.value = this.musicMuted ? 0 : 0.36;
    };

    /** A one-second white-noise buffer, generated once and reused everywhere. */
    Sound.noiseBuffer = function () {
        if (this._noise) return this._noise;
        var ctx = this.ctx;
        var len = ctx.sampleRate;
        var buf = ctx.createBuffer(1, len, ctx.sampleRate);
        var d = buf.getChannelData(0);
        var rng = U.makeRng(1993);
        for (var i = 0; i < len; i++) d[i] = rng() * 2 - 1;
        this._noise = buf;
        return buf;
    };

    // ------------------------------------------------------------- primitives

    /** Filtered noise burst with an exponential decay -- guns and impacts. */
    function noiseHit(S, t, opts) {
        var ctx = S.ctx;
        var src = ctx.createBufferSource();
        src.buffer = S.noiseBuffer();
        src.loop = true;
        src.playbackRate.value = opts.rate || 1;

        var filt = ctx.createBiquadFilter();
        filt.type = opts.filterType || 'lowpass';
        filt.frequency.setValueAtTime(opts.f0, t);
        if (opts.f1 !== undefined) filt.frequency.exponentialRampToValueAtTime(Math.max(40, opts.f1), t + opts.dur);
        filt.Q.value = opts.q === undefined ? 1 : opts.q;

        var g = ctx.createGain();
        g.gain.setValueAtTime(0, t);
        g.gain.linearRampToValueAtTime(opts.gain, t + (opts.attack || 0.002));
        g.gain.exponentialRampToValueAtTime(0.0001, t + opts.dur);

        src.connect(filt);
        filt.connect(g);
        g.connect(opts.dest || S.sfxBus);
        src.start(t);
        src.stop(t + opts.dur + 0.02);
        return g;
    }

    /** Pitched oscillator with an optional glide -- tones, groans, zaps. */
    function tone(S, t, opts) {
        var ctx = S.ctx;
        var o = ctx.createOscillator();
        o.type = opts.type || 'square';
        o.frequency.setValueAtTime(opts.f0, t);
        if (opts.f1 !== undefined) {
            if (opts.linear) o.frequency.linearRampToValueAtTime(opts.f1, t + opts.dur);
            else o.frequency.exponentialRampToValueAtTime(Math.max(1, opts.f1), t + opts.dur);
        }
        var g = ctx.createGain();
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(Math.max(0.0002, opts.gain), t + (opts.attack || 0.006));
        if (opts.hold) g.gain.setValueAtTime(Math.max(0.0002, opts.gain), t + opts.hold);
        g.gain.exponentialRampToValueAtTime(0.0001, t + opts.dur);

        var node = o;
        if (opts.filter) {
            var f = ctx.createBiquadFilter();
            f.type = opts.filter;
            f.frequency.setValueAtTime(opts.filterF0 || 1200, t);
            if (opts.filterF1) f.frequency.exponentialRampToValueAtTime(opts.filterF1, t + opts.dur);
            f.Q.value = opts.filterQ || 1;
            node.connect(f);
            node = f;
        }
        node.connect(g);
        g.connect(opts.dest || S.sfxBus);
        o.start(t);
        o.stop(t + opts.dur + 0.02);
        return o;
    }

    /**
     * Ring-modulated growl: two detuned saws through a moving formant filter.
     * This is the backbone of every monster vocalisation.
     */
    function growl(S, t, opts) {
        var ctx = S.ctx;
        var g = ctx.createGain();
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(opts.gain, t + 0.05);
        g.gain.setValueAtTime(opts.gain, t + opts.dur * 0.6);
        g.gain.exponentialRampToValueAtTime(0.0001, t + opts.dur);

        var formant = ctx.createBiquadFilter();
        formant.type = 'bandpass';
        formant.frequency.setValueAtTime(opts.formant || 500, t);
        formant.frequency.linearRampToValueAtTime((opts.formant || 500) * (opts.formantEnd || 0.6), t + opts.dur);
        formant.Q.value = 4;
        formant.connect(g);
        g.connect(opts.dest || S.sfxBus);

        for (var i = 0; i < 3; i++) {
            var o = ctx.createOscillator();
            o.type = i === 2 ? 'square' : 'sawtooth';
            var f = opts.f0 * (1 + i * 0.007) * (i === 2 ? 0.5 : 1);
            o.frequency.setValueAtTime(f, t);
            o.frequency.exponentialRampToValueAtTime(Math.max(20, f * (opts.bend || 0.7)), t + opts.dur);
            // slow wobble gives the vocal-cord rasp
            var lfo = ctx.createOscillator();
            lfo.frequency.value = opts.rasp || 26;
            var lfoG = ctx.createGain();
            lfoG.gain.value = f * 0.22;
            lfo.connect(lfoG);
            lfoG.connect(o.frequency);
            lfo.start(t); lfo.stop(t + opts.dur + 0.02);
            o.connect(formant);
            o.start(t);
            o.stop(t + opts.dur + 0.02);
        }
        // breath layer
        noiseHit(S, t, {
            f0: (opts.formant || 500) * 2, f1: (opts.formant || 500) * 0.7,
            dur: opts.dur, gain: opts.gain * 0.5, filterType: 'bandpass', q: 2,
            dest: opts.dest || S.sfxBus
        });
        return g;
    }

    // ------------------------------------------------------------------- SFX

    var SFX = {
        pistol: function (S, t, v) {
            noiseHit(S, t, { f0: 6000, f1: 260, dur: 0.16, gain: 0.5 * v, q: 1.2 });
            tone(S, t, { type: 'square', f0: 420, f1: 60, dur: 0.09, gain: 0.28 * v });
            noiseHit(S, t + 0.005, { f0: 2400, f1: 900, dur: 0.05, gain: 0.22 * v, filterType: 'bandpass', q: 3 });
        },
        shotgun: function (S, t, v) {
            noiseHit(S, t, { f0: 4200, f1: 120, dur: 0.42, gain: 0.75 * v, q: 0.8 });
            noiseHit(S, t, { f0: 700, f1: 60, dur: 0.5, gain: 0.55 * v, filterType: 'lowpass' });
            tone(S, t, { type: 'square', f0: 220, f1: 34, dur: 0.2, gain: 0.35 * v });
            // pump action a beat later
            noiseHit(S, t + 0.42, { f0: 2600, f1: 1300, dur: 0.07, gain: 0.18 * v, filterType: 'bandpass', q: 5 });
            noiseHit(S, t + 0.56, { f0: 1800, f1: 900, dur: 0.07, gain: 0.16 * v, filterType: 'bandpass', q: 5 });
        },
        chaingun: function (S, t, v) {
            noiseHit(S, t, { f0: 5200, f1: 300, dur: 0.11, gain: 0.42 * v, q: 1.4 });
            tone(S, t, { type: 'square', f0: 360, f1: 70, dur: 0.07, gain: 0.2 * v });
        },
        chainspin: function (S, t, v) {
            tone(S, t, { type: 'sawtooth', f0: 60, f1: 190, dur: 0.32, gain: 0.16 * v, filter: 'lowpass', filterF0: 400, filterF1: 1400 });
        },
        plasma: function (S, t, v) {
            tone(S, t, { type: 'square', f0: 1500, f1: 240, dur: 0.16, gain: 0.3 * v, filter: 'bandpass', filterF0: 2400, filterF1: 500, filterQ: 6 });
            tone(S, t, { type: 'sawtooth', f0: 900, f1: 160, dur: 0.14, gain: 0.16 * v });
            noiseHit(S, t, { f0: 3600, f1: 800, dur: 0.12, gain: 0.2 * v, filterType: 'bandpass', q: 4 });
        },
        plasmahit: function (S, t, v) {
            noiseHit(S, t, { f0: 5000, f1: 700, dur: 0.14, gain: 0.28 * v, filterType: 'bandpass', q: 2 });
            tone(S, t, { type: 'triangle', f0: 700, f1: 120, dur: 0.12, gain: 0.16 * v });
        },
        punch: function (S, t, v) {
            noiseHit(S, t, { f0: 900, f1: 90, dur: 0.13, gain: 0.4 * v });
            tone(S, t, { type: 'sine', f0: 180, f1: 50, dur: 0.11, gain: 0.3 * v });
        },
        swish: function (S, t, v) {
            noiseHit(S, t, { f0: 500, f1: 2600, dur: 0.13, gain: 0.14 * v, filterType: 'bandpass', q: 1.4 });
        },
        wallhit: function (S, t, v) {
            noiseHit(S, t, { f0: 3000, f1: 500, dur: 0.09, gain: 0.22 * v, filterType: 'bandpass', q: 2 });
        },
        fleshhit: function (S, t, v) {
            noiseHit(S, t, { f0: 1200, f1: 160, dur: 0.12, gain: 0.3 * v });
            tone(S, t, { type: 'sine', f0: 140, f1: 60, dur: 0.1, gain: 0.18 * v });
        },
        fireball: function (S, t, v) {
            noiseHit(S, t, { f0: 400, f1: 1800, dur: 0.3, gain: 0.26 * v, filterType: 'bandpass', q: 1.2 });
            tone(S, t, { type: 'sawtooth', f0: 160, f1: 420, dur: 0.28, gain: 0.14 * v });
        },
        explosion: function (S, t, v) {
            noiseHit(S, t, { f0: 2400, f1: 50, dur: 0.85, gain: 0.85 * v, q: 0.7 });
            noiseHit(S, t + 0.02, { f0: 300, f1: 40, dur: 1.1, gain: 0.6 * v });
            tone(S, t, { type: 'triangle', f0: 130, f1: 24, dur: 0.55, gain: 0.5 * v });
            tone(S, t, { type: 'square', f0: 70, f1: 20, dur: 0.7, gain: 0.3 * v });
        },
        doorOpen: function (S, t, v) {
            tone(S, t, { type: 'sawtooth', f0: 52, f1: 96, dur: 1.0, gain: 0.16 * v, filter: 'lowpass', filterF0: 300, filterF1: 900 });
            noiseHit(S, t, { f0: 240, f1: 1100, dur: 1.0, gain: 0.16 * v, filterType: 'bandpass', q: 2.2 });
            noiseHit(S, t + 0.98, { f0: 900, f1: 200, dur: 0.14, gain: 0.2 * v });
        },
        doorClose: function (S, t, v) {
            tone(S, t, { type: 'sawtooth', f0: 96, f1: 44, dur: 0.9, gain: 0.15 * v, filter: 'lowpass', filterF0: 800, filterF1: 220 });
            noiseHit(S, t + 0.88, { f0: 1400, f1: 120, dur: 0.24, gain: 0.34 * v });
        },
        locked: function (S, t, v) {
            tone(S, t, { type: 'square', f0: 300, f1: 150, dur: 0.1, gain: 0.2 * v });
            tone(S, t + 0.12, { type: 'square', f0: 300, f1: 150, dur: 0.1, gain: 0.2 * v });
        },
        switchFlip: function (S, t, v) {
            noiseHit(S, t, { f0: 3400, f1: 800, dur: 0.06, gain: 0.3 * v, filterType: 'bandpass', q: 6 });
            tone(S, t + 0.03, { type: 'square', f0: 900, f1: 300, dur: 0.12, gain: 0.16 * v });
        },
        pickup: function (S, t, v) {
            tone(S, t, { type: 'square', f0: 660, f1: 1320, dur: 0.12, gain: 0.16 * v });
            tone(S, t + 0.06, { type: 'square', f0: 990, f1: 1760, dur: 0.12, gain: 0.13 * v });
        },
        weaponPickup: function (S, t, v) {
            tone(S, t, { type: 'sawtooth', f0: 220, f1: 660, dur: 0.2, gain: 0.2 * v, filter: 'lowpass', filterF0: 900, filterF1: 3000 });
            tone(S, t + 0.1, { type: 'square', f0: 880, f1: 1320, dur: 0.18, gain: 0.14 * v });
        },
        keyPickup: function (S, t, v) {
            tone(S, t, { type: 'triangle', f0: 880, f1: 1760, dur: 0.22, gain: 0.2 * v });
            tone(S, t + 0.08, { type: 'triangle', f0: 1320, f1: 2640, dur: 0.24, gain: 0.14 * v });
        },
        secret: function (S, t, v) {
            var notes = [523, 659, 784, 1047];
            for (var i = 0; i < notes.length; i++) {
                tone(S, t + i * 0.09, { type: 'square', f0: notes[i], dur: 0.22, gain: 0.14 * v });
            }
        },
        oof: function (S, t, v) {
            growl(S, t, { f0: 150, dur: 0.24, gain: 0.34 * v, formant: 620, bend: 0.6, rasp: 18 });
        },
        playerPain: function (S, t, v) {
            growl(S, t, { f0: 190, dur: 0.36, gain: 0.42 * v, formant: 900, formantEnd: 0.4, bend: 0.55, rasp: 22 });
        },
        playerDeath: function (S, t, v) {
            growl(S, t, { f0: 230, dur: 1.5, gain: 0.55 * v, formant: 1100, formantEnd: 0.22, bend: 0.35, rasp: 14 });
            noiseHit(S, t + 0.5, { f0: 1400, f1: 180, dur: 1.1, gain: 0.24 * v, filterType: 'bandpass', q: 1.4 });
        },
        sightZombie: function (S, t, v) {
            growl(S, t, { f0: 210, dur: 0.5, gain: 0.4 * v, formant: 780, bend: 0.65, rasp: 20 });
        },
        sightImp: function (S, t, v) {
            growl(S, t, { f0: 320, dur: 0.55, gain: 0.4 * v, formant: 1250, formantEnd: 0.45, bend: 0.5, rasp: 32 });
        },
        sightDemon: function (S, t, v) {
            growl(S, t, { f0: 110, dur: 0.7, gain: 0.5 * v, formant: 480, bend: 0.8, rasp: 12 });
        },
        sightBaron: function (S, t, v) {
            growl(S, t, { f0: 78, dur: 1.1, gain: 0.6 * v, formant: 380, formantEnd: 0.5, bend: 0.85, rasp: 9 });
        },
        sightCyber: function (S, t, v) {
            growl(S, t, { f0: 55, dur: 1.5, gain: 0.72 * v, formant: 260, formantEnd: 0.55, bend: 0.9, rasp: 6 });
            tone(S, t, { type: 'square', f0: 40, f1: 30, dur: 1.5, gain: 0.24 * v });
        },
        hoof: function (S, t, v) {
            noiseHit(S, t, { f0: 380, f1: 70, dur: 0.16, gain: 0.34 * v });
            tone(S, t, { type: 'sine', f0: 74, f1: 34, dur: 0.2, gain: 0.3 * v });
        },
        monsterPain: function (S, t, v, pitch) {
            growl(S, t, { f0: 260 * (pitch || 1), dur: 0.28, gain: 0.36 * v, formant: 1000 * (pitch || 1), formantEnd: 0.4, bend: 0.5, rasp: 30 });
        },
        monsterDeath: function (S, t, v, pitch) {
            growl(S, t, { f0: 240 * (pitch || 1), dur: 0.95, gain: 0.46 * v, formant: 900 * (pitch || 1), formantEnd: 0.2, bend: 0.3, rasp: 18 });
            noiseHit(S, t + 0.3, { f0: 1100, f1: 140, dur: 0.7, gain: 0.2 * v, filterType: 'bandpass', q: 1.5 });
        },
        gib: function (S, t, v) {
            noiseHit(S, t, { f0: 1600, f1: 90, dur: 0.4, gain: 0.5 * v });
            for (var i = 0; i < 5; i++) {
                noiseHit(S, t + 0.04 + i * 0.05, { f0: 900, f1: 200, dur: 0.1, gain: 0.2 * v, filterType: 'bandpass', q: 2 });
            }
        },
        teleport: function (S, t, v) {
            tone(S, t, { type: 'sawtooth', f0: 120, f1: 2400, dur: 0.6, gain: 0.22 * v, filter: 'bandpass', filterF0: 400, filterF1: 4000, filterQ: 5 });
            noiseHit(S, t, { f0: 200, f1: 6000, dur: 0.6, gain: 0.2 * v, filterType: 'bandpass', q: 3 });
        },
        nukageBurn: function (S, t, v) {
            noiseHit(S, t, { f0: 900, f1: 260, dur: 0.3, gain: 0.16 * v, filterType: 'bandpass', q: 1.2 });
        },
        menuMove: function (S, t, v) {
            tone(S, t, { type: 'square', f0: 520, f1: 660, dur: 0.06, gain: 0.12 * v });
        },
        menuSelect: function (S, t, v) {
            tone(S, t, { type: 'square', f0: 330, f1: 880, dur: 0.16, gain: 0.16 * v });
        },
        levelDone: function (S, t, v) {
            var seq = [262, 330, 392, 523, 659];
            for (var i = 0; i < seq.length; i++) {
                tone(S, t + i * 0.13, { type: 'sawtooth', f0: seq[i], dur: 0.3, gain: 0.16 * v, filter: 'lowpass', filterF0: 2400, filterF1: 900 });
            }
        }
    };

    /**
     * Fire a sound effect.
     * @param {string} name  key in SFX
     * @param {number} [vol] 0..1 gain scale, typically distance attenuation
     * @param {number} [pitch] optional pitch multiplier for the vocal sounds
     */
    Sound.play = function (name, vol, pitch) {
        if (!this._ready || this.muted) return;
        var fn = SFX[name];
        if (!fn) return;
        var v = vol === undefined ? 1 : U.clamp(vol, 0, 1);
        if (v <= 0.004) return;
        try {
            fn(this, this.ctx.currentTime + 0.001, v, pitch);
        } catch (e) { /* an exhausted audio graph must never break the game */ }
    };

    /** Distance attenuation matching the renderer's falloff. */
    Sound.playAt = function (name, distance, pitch) {
        var v = 1 / (1 + distance * distance * 0.055);
        this.play(name, v, pitch);
    };

    DOOM.Sound = Sound;
    DOOM.SFX_NAMES = Object.keys(SFX);

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 05_music.js --- */

/* ==========================================================================
 * DOOM :: music.js -- procedural heavy metal / dark ambient level tracks
 *
 * A lookahead step sequencer drives three synth voices: a distorted twin-saw
 * guitar through a waveshaper, a filtered square bass, and a noise drum kit.
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;

    var REST = -1;

    /** MIDI note number -> Hz. */
    function midi(n) { return 440 * Math.pow(2, (n - 69) / 12); }

    // ---------------------------------------------------------------- tracks
    //
    // Each track is 32 sixteenth-note steps per bar-pair. Patterns repeat and
    // cycle through `sections` so a level's music evolves instead of looping
    // a single riff for ten minutes.

    var E2 = 40, F2 = 41, Fs2 = 42, G2 = 43, Gs2 = 44, A2 = 45, As2 = 46, B2 = 47;
    var C3 = 48, Cs3 = 49, D3 = 50, E3 = 52, F3 = 53, G3 = 55, A3 = 57, As3 = 58, B3 = 59;
    var C4 = 60, D4 = 62, E4 = 64, G4 = 67, A4 = 69, As4 = 70, B4 = 71;

    var _ = REST;

    var TRACKS = {
        /* E1M1 -- relentless E-pedal gallop, the "at the gate" energy. */
        hangar: {
            bpm: 160,
            drive: 0.85,
            sections: [
                {
                    guitar: [E2, E2, E2, _, E2, _, E2, G2, E2, E2, E2, _, As2, _, A2, G2,
                             E2, E2, E2, _, E2, _, E2, G2, E2, E2, A2, _, G2, _, Fs2, _],
                    bass:   [E2, _, E2, _, E2, _, E2, _, E2, _, E2, _, As2, _, A2, _,
                             E2, _, E2, _, E2, _, E2, _, E2, _, A2, _, G2, _, Fs2, _],
                    lead: null
                },
                {
                    guitar: [G2, G2, G2, _, G2, _, As2, _, A2, A2, A2, _, C3, _, As2, A2,
                             E2, E2, E2, _, E2, _, E2, G2, E2, E2, E2, _, As2, A2, G2, E2],
                    bass:   [G2, _, G2, _, G2, _, As2, _, A2, _, A2, _, C3, _, As2, _,
                             E2, _, E2, _, E2, _, E2, _, E2, _, E2, _, As2, _, G2, _],
                    lead:   [E4, _, D4, _, C4, _, B3, _, _, _, _, _, _, _, _, _,
                             G4, _, _, E4, _, _, D4, _, C4, _, B3, _, A3, _, _, _]
                }
            ],
            drums: {
                kick:  [1,0,0,1, 0,0,1,0, 1,0,0,1, 0,0,1,0, 1,0,0,1, 0,0,1,0, 1,0,1,0, 1,0,1,1],
                snare: [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,1,0],
                hat:   [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,1]
            }
        },

        /* E1M2 -- slower, sludgier, creeping chromatic menace. */
        nuclear: {
            bpm: 132,
            drive: 0.92,
            sections: [
                {
                    guitar: [E2, _, _, E2, _, F2, _, _, E2, _, _, E2, _, As2, _, A2,
                             E2, _, _, E2, _, F2, _, _, Gs2, _, G2, _, Fs2, _, F2, _],
                    bass:   [E2, _, _, _, _, _, _, _, E2, _, _, _, As2, _, _, _,
                             E2, _, _, _, _, _, _, _, Gs2, _, _, _, Fs2, _, _, _],
                    lead: null
                },
                {
                    guitar: [C3, _, _, B2, _, _, As2, _, A2, _, _, Gs2, _, _, G2, _,
                             Fs2, _, F2, _, E2, _, _, _, E2, E2, _, E2, _, F2, _, Fs2],
                    bass:   [C3, _, _, _, As2, _, _, _, A2, _, _, _, Gs2, _, _, _,
                             Fs2, _, _, _, E2, _, _, _, E2, _, _, _, Fs2, _, _, _],
                    lead:   [_, _, _, _, _, _, _, _, B3, _, As3, _, A3, _, _, _,
                             _, _, _, _, _, _, _, _, E3, _, F3, _, E3, _, _, _]
                }
            ],
            drums: {
                kick:  [1,0,0,0, 0,0,1,0, 1,0,0,0, 1,0,0,0, 1,0,0,0, 0,0,1,0, 1,0,0,1, 0,0,0,0],
                snare: [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,1],
                hat:   [1,0,0,1, 0,0,1,0, 1,0,0,1, 0,0,1,0, 1,0,0,1, 0,0,1,0, 1,0,0,1, 0,1,0,1]
            }
        },

        /* E1M8 -- "Sign of Evil": tritone dirge, ritual toms, no groove. */
        anomaly: {
            bpm: 96,
            drive: 1.0,
            sections: [
                {
                    guitar: [E2, _, _, _, _, _, _, _, As2, _, _, _, _, _, _, _,
                             E2, _, _, _, _, _, _, _, B2, _, As2, _, A2, _, _, _],
                    bass:   [E2, _, _, _, _, _, _, _, As2, _, _, _, _, _, _, _,
                             E2, _, _, _, _, _, _, _, As2, _, _, _, _, _, _, _],
                    lead:   [E4, _, _, _, As4, _, _, _, _, _, _, _, _, _, _, _,
                             B4, _, _, As4, _, _, A4, _, _, _, _, _, _, _, _, _]
                },
                {
                    guitar: [C3, _, _, _, B2, _, _, _, As2, _, _, _, A2, _, _, _,
                             Gs2, _, _, _, G2, _, _, _, Fs2, _, _, _, F2, _, E2, _],
                    bass:   [C3, _, _, _, _, _, _, _, As2, _, _, _, _, _, _, _,
                             Gs2, _, _, _, _, _, _, _, Fs2, _, _, _, E2, _, _, _],
                    lead:   [_, _, _, _, _, _, _, _, Cs3, _, _, _, _, _, _, _,
                             E3, _, _, _, _, _, _, _, As3, _, _, _, _, _, _, _]
                }
            ],
            drums: {
                kick:  [1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 1,0,1,0],
                snare: [0,0,0,0, 0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 1,0,0,0],
                hat:   [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0],
                tom:   [1,0,0,1, 0,0,1,0, 0,0,0,0, 0,0,0,0, 1,0,0,1, 0,0,1,0, 0,0,0,0, 0,0,0,0]
            }
        },

        /* Title screen -- ominous, sparse, waiting. */
        title: {
            bpm: 108,
            drive: 0.7,
            sections: [
                {
                    guitar: [E2, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _,
                             As2, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
                    bass:   [E2, _, _, _, _, _, _, _, E2, _, _, _, _, _, _, _,
                             As2, _, _, _, _, _, _, _, As2, _, _, _, _, _, _, _],
                    lead:   [_, _, _, _, E4, _, _, _, _, _, _, _, B3, _, _, _,
                             _, _, _, _, As4, _, _, _, _, _, _, _, _, _, _, _]
                }
            ],
            drums: {
                kick:  [1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0],
                snare: [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0],
                hat:   [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0]
            }
        },

        /* Victory fanfare */
        victory: {
            bpm: 140,
            drive: 0.8,
            sections: [
                {
                    guitar: [E2, E2, G2, _, A2, _, B2, _, C3, _, B2, _, A2, _, G2, _,
                             E2, E2, G2, _, A2, _, B2, _, D3, _, C3, _, B2, _, A2, G2],
                    bass:   [E2, _, G2, _, A2, _, B2, _, C3, _, B2, _, A2, _, G2, _,
                             E2, _, G2, _, A2, _, B2, _, D3, _, C3, _, B2, _, A2, _],
                    lead:   [E4, _, G4, _, A4, _, B4, _, C4, _, B4, _, A4, _, G4, _,
                             E4, _, G4, _, A4, _, B4, _, D4, _, C4, _, B4, _, A4, G4]
                }
            ],
            drums: {
                kick:  [1,0,0,1, 0,0,1,0, 1,0,0,1, 0,0,1,0, 1,0,0,1, 0,0,1,0, 1,0,1,0, 1,0,1,1],
                snare: [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,1,0],
                hat:   [1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,0, 1,0,1,1]
            }
        }
    };

    // ------------------------------------------------------------------ synth

    var Music = {
        current: null,
        _timer: null,
        _step: 0,
        _section: 0,
        _nextTime: 0,
        _shaper: null,
        _playing: false
    };

    /** Asymmetric soft-clip curve -- the "amp" behind the guitar tone. */
    function distortionCurve(ctx, amount) {
        var n = 2048;
        var curve = new Float32Array(n);
        var k = amount * 90 + 8;
        for (var i = 0; i < n; i++) {
            var x = (i * 2) / n - 1;
            curve[i] = ((1 + k) * x) / (1 + k * Math.abs(x));
        }
        return curve;
    }

    Music._guitarChain = function (S, drive) {
        var ctx = S.ctx;
        var shaper = ctx.createWaveShaper();
        shaper.curve = distortionCurve(ctx, drive);
        shaper.oversample = '2x';
        var cab = ctx.createBiquadFilter();      // fake 4x12 cabinet
        cab.type = 'lowpass';
        cab.frequency.value = 2600;
        cab.Q.value = 0.9;
        var pres = ctx.createBiquadFilter();
        pres.type = 'peaking';
        pres.frequency.value = 1400;
        pres.gain.value = 6;
        pres.Q.value = 1.2;
        shaper.connect(cab);
        cab.connect(pres);
        pres.connect(S.musicBus);
        return shaper;
    };

    Music.playNote = function (S, dest, t, note, dur, gain, type, detune) {
        var ctx = S.ctx;
        var f = midi(note);
        var g = ctx.createGain();
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(gain, t + 0.008);
        g.gain.setValueAtTime(gain, t + dur * 0.55);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        g.connect(dest);
        var voices = detune ? 2 : 1;
        for (var i = 0; i < voices; i++) {
            var o = ctx.createOscillator();
            o.type = type;
            o.frequency.value = f;
            if (detune) o.detune.value = (i === 0 ? -8 : 8);
            o.connect(g);
            o.start(t);
            o.stop(t + dur + 0.02);
        }
    };

    Music.kick = function (S, t) {
        var ctx = S.ctx;
        var o = ctx.createOscillator();
        o.type = 'sine';
        o.frequency.setValueAtTime(150, t);
        o.frequency.exponentialRampToValueAtTime(38, t + 0.11);
        var g = ctx.createGain();
        g.gain.setValueAtTime(0.9, t);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.24);
        o.connect(g);
        g.connect(S.musicBus);
        o.start(t); o.stop(t + 0.26);
        // beater click
        var n = ctx.createBufferSource();
        n.buffer = S.noiseBuffer();
        var f = ctx.createBiquadFilter();
        f.type = 'lowpass'; f.frequency.value = 3200;
        var ng = ctx.createGain();
        ng.gain.setValueAtTime(0.35, t);
        ng.gain.exponentialRampToValueAtTime(0.0001, t + 0.03);
        n.connect(f); f.connect(ng); ng.connect(S.musicBus);
        n.start(t); n.stop(t + 0.05);
    };

    Music.snare = function (S, t) {
        var ctx = S.ctx;
        var n = ctx.createBufferSource();
        n.buffer = S.noiseBuffer();
        var f = ctx.createBiquadFilter();
        f.type = 'bandpass'; f.frequency.value = 1900; f.Q.value = 0.7;
        var g = ctx.createGain();
        g.gain.setValueAtTime(0.6, t);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
        n.connect(f); f.connect(g); g.connect(S.musicBus);
        n.start(t); n.stop(t + 0.2);
        var o = ctx.createOscillator();
        o.type = 'triangle';
        o.frequency.setValueAtTime(210, t);
        o.frequency.exponentialRampToValueAtTime(140, t + 0.1);
        var og = ctx.createGain();
        og.gain.setValueAtTime(0.32, t);
        og.gain.exponentialRampToValueAtTime(0.0001, t + 0.13);
        o.connect(og); og.connect(S.musicBus);
        o.start(t); o.stop(t + 0.15);
    };

    Music.hat = function (S, t) {
        var ctx = S.ctx;
        var n = ctx.createBufferSource();
        n.buffer = S.noiseBuffer();
        n.playbackRate.value = 1.9;
        var f = ctx.createBiquadFilter();
        f.type = 'highpass'; f.frequency.value = 7200;
        var g = ctx.createGain();
        g.gain.setValueAtTime(0.16, t);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
        n.connect(f); f.connect(g); g.connect(S.musicBus);
        n.start(t); n.stop(t + 0.06);
    };

    Music.tom = function (S, t) {
        var ctx = S.ctx;
        var o = ctx.createOscillator();
        o.type = 'sine';
        o.frequency.setValueAtTime(190, t);
        o.frequency.exponentialRampToValueAtTime(70, t + 0.3);
        var g = ctx.createGain();
        g.gain.setValueAtTime(0.55, t);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.42);
        o.connect(g); g.connect(S.musicBus);
        o.start(t); o.stop(t + 0.44);
    };

    // -------------------------------------------------------------- sequencer

    var LOOKAHEAD = 0.16;      // seconds of audio scheduled ahead of the clock
    var TICK_MS = 30;

    Music.start = function (trackName) {
        var S = DOOM.Sound;
        if (!S || !S._ready) return;
        if (this.current === trackName && this._playing) return;
        this.stop();
        var track = TRACKS[trackName];
        if (!track) return;
        this.current = trackName;
        this.track = track;
        this._step = 0;
        this._section = 0;
        this._bars = 0;
        this._shaper = this._guitarChain(S, track.drive);
        this._nextTime = S.ctx.currentTime + 0.08;
        this._playing = true;
        var self = this;
        this._timer = setInterval(function () { self._schedule(); }, TICK_MS);
        this._schedule();
    };

    Music._schedule = function () {
        var S = DOOM.Sound;
        if (!this._playing || !S || !S.ctx) return;
        var track = this.track;
        var stepDur = 60 / track.bpm / 4;          // sixteenth notes
        var now = S.ctx.currentTime;
        var guard = 0;
        while (this._nextTime < now + LOOKAHEAD && guard++ < 64) {
            this._emit(S, track, this._step, this._nextTime, stepDur);
            this._nextTime += stepDur;
            this._step++;
            if (this._step >= 32) {
                this._step = 0;
                this._bars++;
                // advance through the sections every two passes
                if (this._bars % 2 === 0 && track.sections.length > 1) {
                    this._section = (this._section + 1) % track.sections.length;
                }
            }
        }
    };

    Music._emit = function (S, track, step, t, stepDur) {
        var sec = track.sections[this._section];
        var d = track.drums;
        var n;

        n = sec.guitar[step];
        if (n !== undefined && n !== REST) {
            this.playNote(S, this._shaper, t, n, stepDur * 1.6, 0.13, 'sawtooth', true);
            // power chord: root + fifth, the one interval metal cannot do without
            this.playNote(S, this._shaper, t, n + 7, stepDur * 1.6, 0.09, 'sawtooth', true);
        }
        n = sec.bass[step];
        if (n !== undefined && n !== REST) {
            var bf = S.ctx.createBiquadFilter();
            bf.type = 'lowpass'; bf.frequency.value = 520;
            bf.connect(S.musicBus);
            this.playNote(S, bf, t, n - 12, stepDur * 2.4, 0.3, 'square', false);
        }
        if (sec.lead) {
            n = sec.lead[step];
            if (n !== undefined && n !== REST) {
                var lf = S.ctx.createBiquadFilter();
                lf.type = 'lowpass'; lf.frequency.value = 3200;
                lf.connect(this._shaper);
                this.playNote(S, lf, t, n, stepDur * 3, 0.08, 'sawtooth', true);
            }
        }
        if (d.kick[step]) this.kick(S, t);
        if (d.snare[step]) this.snare(S, t);
        if (d.hat && d.hat[step]) this.hat(S, t);
        if (d.tom && d.tom[step]) this.tom(S, t);
    };

    Music.stop = function () {
        if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
        }
        this._playing = false;
        this.current = null;
        this._shaper = null;
    };

    DOOM.Music = Music;
    DOOM.MUSIC_TRACKS = TRACKS;
    DOOM.midi = midi;

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 06_maps.js --- */

/* ==========================================================================
 * DOOM :: maps.js -- the three levels, authored as ASCII art and compiled
 * into flat typed arrays the raycaster can walk quickly.
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var TEX = DOOM.TEX, FLAT = DOOM.FLAT, SKY_FLAT = DOOM.SKY_FLAT;

    /**
     * Legend shared by every map. `solid` tiles block movement and sight,
     * `window` tiles block movement but let the ray (and the eye) through,
     * doors animate, and floor codes drive the flat, damage and secret logic.
     */
    var LEGEND = {
        '#': { solid: 1, tex: TEX.STONE },
        'T': { solid: 1, tex: TEX.TECH },
        'M': { solid: 1, tex: TEX.METAL },
        'C': { solid: 1, tex: TEX.COMPUTER },
        'N': { solid: 1, tex: TEX.NUKEWALL },
        'B': { solid: 1, tex: TEX.BRICK },
        'W': { solid: 1, tex: TEX.WOOD },
        'A': { solid: 1, tex: TEX.MARBLE },
        'K': { solid: 1, tex: TEX.SKULLWALL },
        'S': { solid: 1, tex: TEX.SKIN },
        'P': { solid: 1, tex: TEX.SUPPORT },
        'V': { solid: 1, tex: TEX.GSTONE },
        'X': { solid: 1, tex: TEX.EXITSIGN },
        '!': { solid: 1, tex: TEX.SWITCH, action: 'exit' },
        '=': { solid: 1, tex: TEX.BARS, window: 1 },
        '+': { door: 'none', tex: TEX.DOOR },
        'r': { door: 'red', tex: TEX.DOOR_RED },
        'b': { door: 'blue', tex: TEX.DOOR_BLUE },
        'y': { door: 'yellow', tex: TEX.DOOR_YELLOW },
        '.': { floor: FLAT.FLOOR },
        ',': { floor: FLAT.TECHFLOOR },
        'g': { floor: FLAT.GRATE },
        '~': { floor: FLAT.NUKAGE, damage: 5 },
        '%': { floor: FLAT.BLOOD, damage: 0 },
        'p': { floor: FLAT.PENTAGRAM },
        '^': { floor: FLAT.FLOOR, secret: 1 },
        '*': { floor: FLAT.FLOOR, sky: 1 },
        'o': { floor: FLAT.HELLROCK },
        't': { floor: FLAT.TECHFLOOR, teleport: 1 }
    };

    // Tile flag bits used by the compiled map.
    var F_SOLID = 1, F_WINDOW = 2, F_DOOR = 4, F_DAMAGE = 8,
        F_SECRET = 16, F_SKY = 32, F_TELEPORT = 64, F_SWITCH = 128;

    // ===================================================================== E1M1
    // Hangar: start bay -> zigzag corridor -> courtyard with nukage pool and
    // a window onto it -> supply room -> exit chamber. Armor waits in a
    // secret alcove behind the pool.
    var E1M1_ROWS = [
        '#TCCCCCCT#########################',
        '#........TTTTTTTTTTTT************#',
        '#........TTTTTTTTTTTT********#^^^#',
        '#........T......TTTTT********.^^^#',
        '#........+......TTTTT********#^^^#',
        '#........T......TTTTT************#',
        '#........TTTTT..TT..+************#',
        '#........TTTTT..TT..T************#',
        '#TTT+TTTTTTTTT..TT..=**~~~~~~~~~*#',
        '#TT..TTTTTTTTT..TT..=**~~~~~~~~~*#',
        '#TT..TTTTTTTTT..TT..=**~~~~~~~~~*#',
        '#TT..TTTTTTTTT......=**~~~~~~~~~*#',
        '#TT..TTTTTTTTT......T**~~~~~~~~~*#',
        '#TT..TTTTTTTTTTTTTTTT**~~~~~~~~~*#',
        '#TT..TTTTTTTTTTTTTTTT**~~~~~~~~~*#',
        '#TT..TTTTTTTTTTTTTTTT**~~~~~~~~~*#',
        '#TT..TTTTTTTTTTTTTTTT###+#########',
        '#TT..TTTTTTTTTTTTTTTT###.#########',
        '#TT..TTTTTTTTTTTTTTTT#..........##',
        '#..........TTTTTTTTTTT#,,,,,,,,,##',
        '#..........TTTTTTTTTTT#,,,,,,,,,##',
        '#..........TTTTTTTTTTT#,,,,,,,,,X#',
        '#..........TTTTTTTTTTT#,,,,,,,,,!#',
        '#....................,,,,,,,,,,,X#',
        '#..........TTTTTTTTTTT#,,,,,,,,,##',
        '#..........TTTTTTTTTTT#,,,,,,,,,##',
        '#..........TTTTTTTTTTT#,,,,,,,,,##',
        '#..........TTTTTTTTTTT#..........#',
        '##################################',
        '##################################'
    ];

    // ===================================================================== E1M2
    // Nuclear Plant: computer bank -> blue-locked reactor hall with a nukage
    // sump -> pitch-dark service maze -> yellow-locked ambush arena ->
    // red-locked exit. Three keycards, three locks, one long walk back.
    var E1M2_ROWS = [
        '##################################',
        '#.......MMMMM........M...........#',
        '#.......MMMMM.CCCCCC.M..~~~~~~~..#',
        '#..............CCCC..M..~~~~~~~..#',
        '#.......MMMMM........b..~~~~~~~..#',
        '#.......MMMMM........M..~~~~~~~..#',
        '#.......MMMMM........M...........#',
        '#.......MMMMM.CCCCCC.M...........#',
        '#MMMMMMMMMMMM........M...........#',
        '#MMMMMMMMMMMMMMM..MMMM...........#',
        '#..............M..MMMMMMMMMMMMMMM#',
        '#.MM.MM.MM.MM..M..MMMMMMMMMMMMMMM#',
        '#.M..M...M..M..M..MMMMMMMMMMMMMMM#',
        '#.M.MM.MMM.MM..M..MMMMMMMMMMMMMMM#',
        '#.M....M.....M....MMMMMMMMMMMMMMM#',
        '#.MMMM.M.MMM.M.MMMMMMMMMMMMMMMMMM#',
        '#.....M..M...M.MMM...............#',
        '#MMMM.MMM.MMMM.MMM....MMMMMM.....#',
        '#^^.....M......MMM....MMMMMM.....#',
        '#.MMMMM.M.MMMM.MMM...............#',
        '#................y...............#',
        '#MMMMMMMMMMMMMMMMM...............#',
        '#MMMMMMMMMMMMMMMMM...MMMM........#',
        '#........MMMMMMMMM...MMMM........#',
        'X........MMMMMMMMM...MMMM........#',
        '!........r.......................#',
        'X........MMMMMMMMM#..............#',
        '#........MMMMMMMMM#..............#',
        '##################################',
        '##################################'
    ];

    // ===================================================================== E1M8
    // Phobos Anomaly: a short marble approach opening onto the pentagram
    // arena. Kill what waits there and the teleporter home unseals.
    var E1M8_ROWS = [
        '##################################',
        '#AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA#',
        '#A......AAAAAAAAAAAAAAAAAAAAAAAAA#',
        '#A......AAAAAAAAAAAAAAAAAAAAAAAAA#',
        '#A......A^^^^^AAAAAAAAAAAAAAAAAAA#',
        '#A.............AAAAAAAAAAAAAAAAAA#',
        '#A......A%%%%%AAAAAAAAAAAAAAAAAAA#',
        '#AAAAAAAA.....AAAAAAAAAAAAAAAAAAA#',
        '#AAAAAAAA.....AAAAAAAAAAAAAAAAAAA#',
        '#oooooooo.....ooooooooooooooooooo#',
        '#oo%%oooooooooooooooooooooo%%%%oo#',
        '#oo%%ooKKooooooooooooooKKoo%%%%oo#',
        '#ooooooKKooooooooooooooKKoooooooo#',
        '#ooooooooopppppppppppoooooooooooo#',
        '#ooooooooopppppppppppoooooooooooo#',
        '#ooooooooopppppppppppoooooooooooo#',
        '#ooKKooooopppppppppppoooooooKKooo#',
        '#ooKKooooopppppppppppoooooooKKooo#',
        '#ooooooooopppppppppppoooooooooooo#',
        '#ooooooooopppppppppppoooooooooooo#',
        '#ooooooooopppppppppppo%%%oooooooo#',
        '#oo%%%%ooooooooooooooo%%%oooooooo#',
        '#oo%%%%oooooooooooooooooooKKooooo#',
        '#oooooooooooooottttoooooooooooooo#',
        '#ooKKoooooooooottttoooooooooooooo#',
        '#oooooooooooooottttoooooooooooooo#',
        '#oooooooooooooooooooooooooooooooo#',
        '#oooooooooooooooooooooooooooooooo#',
        '##################################',
        '##################################'
    ];

    // ------------------------------------------------------------- compilation

    /**
     * Turn ASCII rows into typed arrays.
     * Throws on an unknown glyph or a ragged grid -- both are authoring bugs
     * we would much rather see at load time than as a hole in a wall.
     */
    function compile(def) {
        var rows = def.rows;
        var h = rows.length, w = rows[0].length;
        for (var i = 0; i < h; i++) {
            if (rows[i].length !== w) {
                throw new Error('map ' + def.id + ': row ' + i + ' is ' + rows[i].length +
                    ' wide, expected ' + w);
            }
        }
        var n = w * h;
        var map = {
            id: def.id,
            name: def.name,
            music: def.music,
            sky: def.sky,
            light: def.light,
            par: def.par,
            w: w, h: h,
            flags: new Uint8Array(n),
            tex: new Uint8Array(n),
            floor: new Uint8Array(n),
            ceil: new Uint8Array(n),
            damage: new Uint8Array(n),
            doorKey: new Array(n),
            action: new Array(n),
            things: def.things,
            start: def.start,
            defaultCeil: def.ceil === undefined ? FLAT.CEIL : def.ceil,
            bossLevel: !!def.bossLevel,
            secretCount: 0
        };

        for (var y = 0; y < h; y++) {
            for (var x = 0; x < w; x++) {
                var ch = rows[y].charAt(x);
                var d = LEGEND[ch];
                if (!d) throw new Error('map ' + def.id + ': unknown glyph "' + ch + '" at ' + x + ',' + y);
                var idx = y * w + x;
                var f = 0;
                map.tex[idx] = d.tex === undefined ? TEX.STONE : d.tex;
                map.floor[idx] = d.floor === undefined ? def.floorFlat : d.floor;
                map.ceil[idx] = map.defaultCeil;
                if (d.solid) f |= F_SOLID;
                if (d.window) f |= F_WINDOW;
                if (d.door) { f |= F_DOOR; map.doorKey[idx] = d.door; }
                if (d.damage) { f |= F_DAMAGE; map.damage[idx] = d.damage; }
                if (d.secret) { f |= F_SECRET; map.secretCount++; }
                if (d.sky) { f |= F_SKY; map.ceil[idx] = SKY_FLAT; }
                if (d.teleport) f |= F_TELEPORT;
                if (d.action) { f |= F_SWITCH; map.action[idx] = d.action; }
                map.flags[idx] = f;
            }
        }

        // Group adjacent secret tiles into one "secret sector", so a 3x3
        // alcove counts once towards the intermission stats.
        map.secretId = new Int16Array(n);
        map.secretId.fill(-1);
        var groups = 0;
        for (y = 0; y < h; y++) {
            for (x = 0; x < w; x++) {
                var s0 = y * w + x;
                if (!(map.flags[s0] & F_SECRET) || map.secretId[s0] >= 0) continue;
                var stack = [s0];
                map.secretId[s0] = groups;
                while (stack.length) {
                    var cur = stack.pop();
                    var cx = cur % w, cy = (cur / w) | 0;
                    var nb = [[cx + 1, cy], [cx - 1, cy], [cx, cy + 1], [cx, cy - 1]];
                    for (var k = 0; k < 4; k++) {
                        var nx = nb[k][0], ny = nb[k][1];
                        if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
                        var ni = ny * w + nx;
                        if ((map.flags[ni] & F_SECRET) && map.secretId[ni] < 0) {
                            map.secretId[ni] = groups;
                            stack.push(ni);
                        }
                    }
                }
                groups++;
            }
        }
        map.secretCount = groups;

        // Ceiling lights: sprinkle bright panels through indoor rooms so the
        // ceiling is not a uniform slab.
        for (y = 2; y < h - 2; y += 6) {
            for (x = 2; x < w - 2; x += 6) {
                var id2 = y * w + x;
                if (!(map.flags[id2] & (F_SOLID | F_SKY))) map.ceil[id2] = FLAT.CEILLIGHT;
            }
        }
        return map;
    }

    function idx(map, x, y) { return y * map.w + x; }

    function inBounds(map, x, y) { return x >= 0 && y >= 0 && x < map.w && y < map.h; }

    function tileFlags(map, x, y) {
        if (!inBounds(map, x, y)) return F_SOLID;
        return map.flags[y * map.w + x];
    }

    /** True when the tile stops the player, ignoring door animation state. */
    function isBlocking(map, x, y) {
        var f = tileFlags(map, x, y);
        return !!(f & (F_SOLID | F_WINDOW));
    }

    // ----------------------------------------------------------------- things

    var E1M1_THINGS = [
        { t: 'zombieman', x: 12.5, y: 4.5, skill: 1 },
        { t: 'zombieman', x: 14.5, y: 10.5, skill: 1 },
        { t: 'zombieman', x: 18.5, y: 11.5, skill: 2 },
        { t: 'sergeant', x: 5.5, y: 22.5, skill: 2 },
        { t: 'sergeant', x: 26.5, y: 20.5, skill: 3 },
        { t: 'imp', x: 25.5, y: 4.5, skill: 1 },
        { t: 'imp', x: 30.5, y: 14.5, skill: 2 },
        { t: 'imp', x: 27.5, y: 25.5, skill: 3 },
        { t: 'demon', x: 8.5, y: 25.5, skill: 3 },
        { t: 'barrel', x: 18.5, y: 12.5 },
        { t: 'barrel', x: 19.5, y: 12.5 },
        { t: 'barrel', x: 24.5, y: 19.5 },
        { t: 'clip', x: 13.5, y: 4.5 },
        { t: 'clip', x: 6.5, y: 6.5 },
        { t: 'shells', x: 15.5, y: 11.5 },
        { t: 'ammobox', x: 30.5, y: 2.5 },
        { t: 'medikit', x: 2.5, y: 20.5 },
        { t: 'stimpack', x: 9.5, y: 26.5 },
        { t: 'stimpack', x: 3.5, y: 3.5 },
        { t: 'shotgun', x: 3.5, y: 25.5 },
        { t: 'armor', x: 31.5, y: 3.5 },
        { t: 'lamp', x: 22.5, y: 2.5 },
        { t: 'lamp', x: 22.5, y: 14.5 },
        { t: 'lamp', x: 24.5, y: 27.5 },
        { t: 'gore', x: 24.5, y: 27.5 }
    ];

    var E1M2_THINGS = [
        { t: 'zombieman', x: 10.5, y: 3.5, skill: 1 },
        { t: 'zombieman', x: 15.5, y: 1.5, skill: 1 },
        { t: 'zombieman', x: 4.5, y: 10.5, skill: 2 },
        { t: 'sergeant', x: 18.5, y: 5.5, skill: 1 },
        { t: 'sergeant', x: 30.5, y: 3.5, skill: 2 },
        { t: 'sergeant', x: 24.5, y: 16.5, skill: 2 },
        { t: 'imp', x: 27.5, y: 6.5, skill: 1 },
        { t: 'imp', x: 3.5, y: 18.5, skill: 1 },
        { t: 'imp', x: 12.5, y: 16.5, skill: 2 },
        { t: 'imp', x: 22.5, y: 20.5, skill: 1 },
        { t: 'imp', x: 30.5, y: 21.5, skill: 2 },
        { t: 'demon', x: 25.5, y: 26.5, skill: 1 },
        { t: 'demon', x: 30.5, y: 16.5, skill: 2 },
        { t: 'demon', x: 20.5, y: 23.5, skill: 3 },
        { t: 'barrel', x: 23.5, y: 2.5 },
        { t: 'barrel', x: 22.5, y: 3.5 },
        { t: 'barrel', x: 26.5, y: 22.5 },
        { t: 'bluekey', x: 1.5, y: 20.5 },
        { t: 'yellowkey', x: 31.5, y: 7.5 },
        { t: 'redkey', x: 31.5, y: 26.5 },
        { t: 'chaingun', x: 19.5, y: 1.5 },
        { t: 'shotgun', x: 13.5, y: 10.5 },
        { t: 'medikit', x: 1.5, y: 16.5 },
        { t: 'medikit', x: 28.5, y: 18.5 },
        { t: 'stimpack', x: 6.5, y: 3.5 },
        { t: 'stimpack', x: 2.5, y: 25.5 },
        { t: 'armor', x: 5.5, y: 6.5 },
        { t: 'ammobox', x: 19.5, y: 16.5 },
        { t: 'shellbox', x: 29.5, y: 24.5 },
        { t: 'clip', x: 11.5, y: 12.5 },
        { t: 'shells', x: 4.5, y: 16.5 },
        { t: 'lamp', x: 20.5, y: 1.5 },
        { t: 'lamp', x: 3.5, y: 25.5 },
        { t: 'gore', x: 27.5, y: 22.5 }
    ];

    var E1M8_THINGS = [
        { t: 'imp', x: 4.5, y: 10.5, skill: 1 },
        { t: 'imp', x: 28.5, y: 10.5, skill: 1 },
        { t: 'imp', x: 3.5, y: 21.5, skill: 2 },
        { t: 'imp', x: 28.5, y: 21.5, skill: 2 },
        { t: 'demon', x: 8.5, y: 15.5, skill: 1 },
        { t: 'demon', x: 24.5, y: 15.5, skill: 1 },
        { t: 'demon', x: 16.5, y: 26.5, skill: 3 },
        { t: 'sergeant', x: 2.5, y: 26.5, skill: 2 },
        { t: 'sergeant', x: 29.5, y: 26.5, skill: 2 },
        { t: 'baron', x: 6.5, y: 12.5, skill: 1 },
        { t: 'baron', x: 25.5, y: 12.5, skill: 1 },
        { t: 'cyberdemon', x: 16.5, y: 12.5, skill: 1, boss: true },
        { t: 'soulsphere', x: 11.5, y: 4.5 },
        { t: 'megaarmor', x: 4.5, y: 4.5 },
        { t: 'plasma', x: 12.5, y: 5.5 },
        { t: 'cellpack', x: 2.5, y: 5.5 },
        { t: 'cellpack', x: 6.5, y: 5.5 },
        { t: 'medikit', x: 2.5, y: 9.5 },
        { t: 'medikit', x: 29.5, y: 9.5 },
        { t: 'medikit', x: 16.5, y: 27.5 },
        { t: 'shellbox', x: 12.5, y: 10.5 },
        { t: 'shellbox', x: 20.5, y: 10.5 },
        { t: 'ammobox', x: 3.5, y: 3.5 },
        { t: 'gore', x: 2.5, y: 11.5 },
        { t: 'gore', x: 29.5, y: 11.5 },
        { t: 'lamp', x: 16.5, y: 9.5 }
    ];

    var DEFS = [
        {
            id: 'E1M1', name: 'E1M1: HANGAR', music: 'hangar', sky: 'tech',
            light: 0.86, par: 30, rows: E1M1_ROWS, floorFlat: FLAT.FLOOR,
            ceil: FLAT.CEIL, start: { x: 4.5, y: 4.5, a: 0 }, things: E1M1_THINGS
        },
        {
            id: 'E1M2', name: 'E1M2: NUCLEAR PLANT', music: 'nuclear', sky: 'tech',
            light: 0.5, par: 75, rows: E1M2_ROWS, floorFlat: FLAT.TECHFLOOR,
            ceil: FLAT.CEIL, start: { x: 3.5, y: 3.5, a: 0 }, things: E1M2_THINGS
        },
        {
            id: 'E1M8', name: 'E1M8: PHOBOS ANOMALY', music: 'anomaly', sky: 'hell',
            light: 0.62, par: 120, rows: E1M8_ROWS, floorFlat: FLAT.HELLROCK,
            ceil: FLAT.HELLCEIL, start: { x: 4.5, y: 4.5, a: 0 },
            things: E1M8_THINGS, bossLevel: true
        }
    ];

    function load(index) { return compile(DEFS[index]); }

    DOOM.Maps = {
        LEGEND: LEGEND,
        DEFS: DEFS,
        count: DEFS.length,
        compile: compile,
        load: load,
        idx: idx,
        inBounds: inBounds,
        tileFlags: tileFlags,
        isBlocking: isBlocking,
        F_SOLID: F_SOLID, F_WINDOW: F_WINDOW, F_DOOR: F_DOOR, F_DAMAGE: F_DAMAGE,
        F_SECRET: F_SECRET, F_SKY: F_SKY, F_TELEPORT: F_TELEPORT, F_SWITCH: F_SWITCH
    };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 07_raycaster.js --- */

/* ==========================================================================
 * DOOM :: raycaster.js -- the pseudo-3D renderer
 *
 * Per frame:
 *   1. cast floors and ceilings (or sky) across the whole screen,
 *   2. DDA-cast one wall ray per column, collecting hits back-to-front so
 *      windows and half-open doors show what is behind them,
 *   3. blit depth-sorted billboard sprites against the column z-buffer.
 *
 * Everything writes 32-bit ABGR words straight into an ImageData buffer, and
 * every shade is a colormap lookup, so a pixel costs one array read.
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;
    var M = DOOM.Maps;
    var CM = U.COLORMAP;
    var TSIZE = DOOM.TEX_SIZE;
    var SKY_FLAT = DOOM.SKY_FLAT;

    var MAX_HITS = 8;          // ray gives up after this many transparent tiles
    var MAX_DEPTH = 42;        // map units

    function Renderer(w, h) {
        this.w = w;
        this.h = h;
        this.buf = new Uint32Array(w * h);
        this.zbuf = new Float32Array(w);
        this.assets = null;
        // scratch arrays for the per-column hit list, reused every ray
        this._hd = new Float64Array(MAX_HITS);   // perpendicular distance
        this._ht = new Int32Array(MAX_HITS);     // texture id
        this._hx = new Float64Array(MAX_HITS);   // texture u in 0..1
        this._hs = new Int32Array(MAX_HITS);     // side (0 = x-face, 1 = y-face)
        this._hk = new Int32Array(MAX_HITS);     // kind: 0 wall, 1 door, 2 window
        this._ho = new Float64Array(MAX_HITS);   // door openness
    }

    Renderer.prototype.setAssets = function (textures, sprites) {
        this.assets = textures;
        this.sprites = sprites;
    };

    /**
     * @param {object} sc scene description:
     *   map, px, py, ang, fov, light, doorOpenness(fn), tick, horizon, sky
     */
    Renderer.prototype.render = function (sc) {
        this.castPlanes(sc);
        this.castWalls(sc);
        return this.buf;
    };

    // ------------------------------------------------------- floors & ceilings

    Renderer.prototype.castPlanes = function (sc) {
        var w = this.w, h = this.h, buf = this.buf;
        var map = sc.map, tex = this.assets;
        var flats = tex.flats, sky = tex.skies[sc.sky], skyW = tex.skyW, skyH = tex.skyH;
        var animFrame = (sc.tick / 8) | 0;

        var dirX = Math.cos(sc.ang), dirY = Math.sin(sc.ang);
        var planeLen = Math.tan(sc.fov / 2);
        var planeX = -dirY * planeLen, planeY = dirX * planeLen;
        var posX = sc.px, posY = sc.py;
        var horizon = sc.horizon;
        var light = sc.light;
        var mw = map.w, mh = map.h;

        // Sky is projected by view angle so it pans as you turn, and never
        // moves with the horizon -- exactly like the original's sky wall.
        var skyScaleX = skyW / U.PI2;
        var skyBase = U.normAngle(sc.ang - sc.fov / 2) * skyScaleX;
        var skyStepX = (sc.fov * skyScaleX) / w;

        for (var y = 0; y < h; y++) {
            var isFloor = y > horizon;
            var p = isFloor ? (y - horizon) : (horizon - y);
            if (p < 1) p = 1;
            // camera sits at half wall height, so both planes share this
            var rowDist = (h * 0.5) / p;
            if (rowDist > MAX_DEPTH * 1.6) rowDist = MAX_DEPTH * 1.6;

            var rdx0 = dirX - planeX, rdy0 = dirY - planeY;
            var rdx1 = dirX + planeX, rdy1 = dirY + planeY;
            var stepX = rowDist * (rdx1 - rdx0) / w;
            var stepY = rowDist * (rdy1 - rdy0) / w;
            var fx = posX + rowDist * rdx0;
            var fy = posY + rowDist * rdy0;

            var lvl = U.lightForDistance(rowDist, light, isFloor ? 0 : 1);
            var lvlBase = lvl << 8;
            var row = y * w;
            var skyX = skyBase;

            for (var x = 0; x < w; x++, fx += stepX, fy += stepY, skyX += skyStepX) {
                var cx = fx | 0, cy = fy | 0;
                if (fx < 0) cx = -1;
                if (fy < 0) cy = -1;
                var outside = (cx < 0 || cy < 0 || cx >= mw || cy >= mh);
                var cell = outside ? -1 : cy * mw + cx;

                if (!isFloor) {
                    // ceiling: sky tiles sample the panorama instead of a flat
                    if (outside || map.ceil[cell] === SKY_FLAT) {
                        var sxp = ((skyX % skyW) + skyW) % skyW | 0;
                        var syp = (y / horizon) * skyH * 0.92 | 0;
                        if (syp < 0) syp = 0;
                        if (syp >= skyH) syp = skyH - 1;
                        // the sky is drawn unshaded: distance means nothing up there
                        buf[row + x] = CM[sky.px[syp * skyW + sxp]];
                        continue;
                    }
                }
                if (outside) { buf[row + x] = CM[(31 << 8)]; continue; }

                var flatId = isFloor ? map.floor[cell] : map.ceil[cell];
                var variants = flats[flatId];
                var flat = variants[variants.length > 1 ? (animFrame % variants.length) : 0];
                var tx = (fx - cx) * TSIZE | 0;
                var ty = (fy - cy) * TSIZE | 0;
                buf[row + x] = CM[lvlBase | flat.px[ty * TSIZE + tx]];
            }
        }
    };

    // ------------------------------------------------------------------ walls

    Renderer.prototype.castWalls = function (sc) {
        var w = this.w, h = this.h, buf = this.buf, zbuf = this.zbuf;
        var map = sc.map, walls = this.assets.walls;
        var mw = map.w, mh = map.h, flags = map.flags, mtex = map.tex;
        var F_SOLID = M.F_SOLID, F_WINDOW = M.F_WINDOW, F_DOOR = M.F_DOOR;
        var horizon = sc.horizon, light = sc.light;
        var dirX = Math.cos(sc.ang), dirY = Math.sin(sc.ang);
        var planeLen = Math.tan(sc.fov / 2);
        var planeX = -dirY * planeLen, planeY = dirX * planeLen;
        var posX = sc.px, posY = sc.py;
        var hd = this._hd, ht = this._ht, hx = this._hx, hs = this._hs,
            hk = this._hk, ho = this._ho;

        for (var x = 0; x < w; x++) {
            var camX = 2 * x / w - 1;
            var rayX = dirX + planeX * camX;
            var rayY = dirY + planeY * camX;

            var mapX = posX | 0, mapY = posY | 0;
            var deltaX = rayX === 0 ? 1e30 : Math.abs(1 / rayX);
            var deltaY = rayY === 0 ? 1e30 : Math.abs(1 / rayY);
            var stepX, stepY, sideDistX, sideDistY;

            if (rayX < 0) { stepX = -1; sideDistX = (posX - mapX) * deltaX; }
            else { stepX = 1; sideDistX = (mapX + 1 - posX) * deltaX; }
            if (rayY < 0) { stepY = -1; sideDistY = (posY - mapY) * deltaY; }
            else { stepY = 1; sideDistY = (mapY + 1 - posY) * deltaY; }

            var nHits = 0, side = 0, perp = 0;
            while (nHits < MAX_HITS) {
                if (sideDistX < sideDistY) { sideDistX += deltaX; mapX += stepX; side = 0; }
                else { sideDistY += deltaY; mapY += stepY; side = 1; }
                if (mapX < 0 || mapY < 0 || mapX >= mw || mapY >= mh) break;

                var cell = mapY * mw + mapX;
                var f = flags[cell];
                if (!(f & (F_SOLID | F_DOOR))) continue;

                perp = side === 0 ? (sideDistX - deltaX) : (sideDistY - deltaY);
                if (perp > MAX_DEPTH) break;
                if (perp < 0.0001) perp = 0.0001;

                var openness = 0, kind = 0;
                if (f & F_DOOR) {
                    openness = sc.doorOpenness(mapX, mapY);
                    if (openness >= 0.985) continue;      // fully retracted
                    kind = 1;
                } else if (f & F_WINDOW) {
                    kind = 2;
                }

                var wallU = side === 0 ? (posY + perp * rayY) : (posX + perp * rayX);
                wallU -= Math.floor(wallU);
                // keep the texture from mirroring on back faces
                if ((side === 0 && rayX > 0) || (side === 1 && rayY < 0)) wallU = 1 - wallU;

                hd[nHits] = perp;
                ht[nHits] = mtex[cell];
                hx[nHits] = wallU;
                hs[nHits] = side;
                hk[nHits] = kind;
                ho[nHits] = openness;
                nHits++;

                if (kind === 0) break;                    // opaque: stop here
            }

            // nearest opaque hit governs sprite clipping for this column
            zbuf[x] = MAX_DEPTH;
            for (var q = 0; q < nHits; q++) {
                if (hk[q] === 0) { zbuf[x] = hd[q]; break; }
            }

            // painter's algorithm: farthest hit first
            for (var i = nHits - 1; i >= 0; i--) {
                this._drawSlice(x, hd[i], ht[i], hx[i], hs[i], hk[i], ho[i], horizon, light, walls);
            }
        }
    };

    /**
     * Draw one textured vertical slice.
     * Doors render only their unretracted top portion; windows render a solid
     * band at top and bottom with a gap between so you can see through.
     */
    Renderer.prototype._drawSlice = function (x, perp, texId, u, side, kind, openness, horizon, light, walls) {
        var w = this.w, h = this.h, buf = this.buf;
        var lineH = h / perp;
        var top = horizon - lineH * 0.5;
        var lvl = U.lightForDistance(perp, light, side === 1 ? 2 : 0);
        var lvlBase = lvl << 8;
        var tex = walls[texId];
        var texU = (u * TSIZE) | 0;
        if (texU < 0) texU = 0;
        if (texU >= TSIZE) texU = TSIZE - 1;

        var bands;
        if (kind === 1) bands = [[0, 1 - openness]];
        else if (kind === 2) bands = [[0, 0.30], [0.72, 1]];
        else bands = [[0, 1]];

        for (var b = 0; b < bands.length; b++) {
            var v0 = bands[b][0], v1 = bands[b][1];
            if (v1 <= v0) continue;
            var y0 = Math.ceil(top + lineH * v0);
            var y1 = Math.ceil(top + lineH * v1);
            if (y0 < 0) y0 = 0;
            if (y1 > h) y1 = h;
            for (var y = y0; y < y1; y++) {
                var v = (y - top) / lineH;              // 0..1 down the wall
                var ty = (v * TSIZE) | 0;
                if (ty < 0) ty = 0;
                if (ty >= TSIZE) ty = TSIZE - 1;
                buf[y * w + x] = CM[lvlBase | tex.px[ty * TSIZE + texU]];
            }
        }
    };

    // ---------------------------------------------------------------- sprites

    /**
     * @param {Array} list  entries of
     *   {x, y, img, worldH, zOff, light, ghost}
     *   worldH is in wall-heights, zOff lifts the sprite off the floor.
     */
    Renderer.prototype.drawSprites = function (sc, list) {
        var w = this.w, h = this.h, buf = this.buf, zbuf = this.zbuf;
        var dirX = Math.cos(sc.ang), dirY = Math.sin(sc.ang);
        var planeLen = Math.tan(sc.fov / 2);
        var planeX = -dirY * planeLen, planeY = dirX * planeLen;
        var posX = sc.px, posY = sc.py, horizon = sc.horizon;
        var i;

        for (i = 0; i < list.length; i++) {
            var dx = list[i].x - posX, dy = list[i].y - posY;
            list[i]._d = dx * dx + dy * dy;
        }
        list.sort(function (a, b) { return b._d - a._d; });

        var invDet = 1 / (planeX * dirY - dirX * planeY);

        for (i = 0; i < list.length; i++) {
            var s = list[i];
            var img = s.img;
            if (!img) continue;
            var sx = s.x - posX, sy = s.y - posY;
            var tX = invDet * (dirY * sx - dirX * sy);
            var tY = invDet * (-planeY * sx + planeX * sy);
            if (tY < 0.12) continue;                       // behind or inside us

            var lineH = h / tY;
            var screenX = (w * 0.5) * (1 + tX / tY);
            var spriteH = lineH * s.worldH;
            var spriteW = spriteH * (img.w / img.h);
            // anchor: bottom of the billboard rests on the floor line
            var floorY = horizon + lineH * 0.5;
            var bottom = floorY - (s.zOff || 0) * lineH;
            var top = bottom - spriteH;
            var left = screenX - spriteW * 0.5;

            var x0 = Math.ceil(left), x1 = Math.ceil(left + spriteW);
            if (x1 <= 0 || x0 >= w) continue;
            if (x0 < 0) x0 = 0;
            if (x1 > w) x1 = w;
            var y0 = Math.ceil(top), y1 = Math.ceil(top + spriteH);
            if (y1 <= 0 || y0 >= h) continue;
            var cy0 = y0 < 0 ? 0 : y0, cy1 = y1 > h ? h : y1;

            var lvl = U.lightForDistance(tY, sc.light, 0) - (s.light || 0);
            if (lvl < 0) lvl = 0;
            if (lvl > U.LIGHT_LEVELS - 1) lvl = U.LIGHT_LEVELS - 1;
            var lvlBase = lvl << 8;
            var iw = img.w, ih = img.h, px = img.px;
            var invW = iw / spriteW, invH = ih / spriteH;

            for (var x = x0; x < x1; x++) {
                if (tY >= zbuf[x]) continue;                // hidden by a wall
                var tx = ((x - left) * invW) | 0;
                if (tx < 0) tx = 0;
                if (tx >= iw) tx = iw - 1;
                var col = tx;
                for (var y = cy0; y < cy1; y++) {
                    var ty = ((y - top) * invH) | 0;
                    if (ty < 0) ty = 0;
                    if (ty >= ih) ty = ih - 1;
                    var c = px[ty * iw + col];
                    if (c === 255) continue;
                    buf[y * w + x] = CM[lvlBase | c];
                }
            }
        }
    };

    /** Full-screen palette tint, used for damage/pickup/radiation flashes. */
    Renderer.prototype.tint = function (r, g, b, amount) {
        var buf = this.buf, n = buf.length;
        var a = U.clamp(amount, 0, 1);
        var ia = 1 - a;
        var tr = r * a, tg = g * a, tb = b * a;
        for (var i = 0; i < n; i++) {
            var c = buf[i];
            var cr = (c & 255) * ia + tr;
            var cg = ((c >> 8) & 255) * ia + tg;
            var cb = ((c >> 16) & 255) * ia + tb;
            buf[i] = (255 << 24) | ((cb | 0) << 16) | ((cg | 0) << 8) | (cr | 0);
        }
    };

    /**
     * Cast a single ray and return the distance to the first blocking tile,
     * or `max` if nothing is hit. Shared by hitscan weapons and monster sight
     * checks so what the player sees and what the AI sees always agree.
     */
    function rayTrace(map, px, py, ang, max, doorOpenness) {
        var rayX = Math.cos(ang), rayY = Math.sin(ang);
        var mapX = px | 0, mapY = py | 0;
        var deltaX = rayX === 0 ? 1e30 : Math.abs(1 / rayX);
        var deltaY = rayY === 0 ? 1e30 : Math.abs(1 / rayY);
        var stepX, stepY, sideDistX, sideDistY, side = 0;

        if (rayX < 0) { stepX = -1; sideDistX = (px - mapX) * deltaX; }
        else { stepX = 1; sideDistX = (mapX + 1 - px) * deltaX; }
        if (rayY < 0) { stepY = -1; sideDistY = (py - mapY) * deltaY; }
        else { stepY = 1; sideDistY = (mapY + 1 - py) * deltaY; }

        for (var guard = 0; guard < 256; guard++) {
            if (sideDistX < sideDistY) { sideDistX += deltaX; mapX += stepX; side = 0; }
            else { sideDistY += deltaY; mapY += stepY; side = 1; }
            if (mapX < 0 || mapY < 0 || mapX >= map.w || mapY >= map.h) return max;
            var perp = side === 0 ? (sideDistX - deltaX) : (sideDistY - deltaY);
            if (perp > max) return max;
            var f = map.flags[mapY * map.w + mapX];
            if (f & M.F_DOOR) {
                if (doorOpenness && doorOpenness(mapX, mapY) > 0.75) continue;
                return perp;
            }
            if (f & (M.F_SOLID | M.F_WINDOW)) return perp;
        }
        return max;
    }

    /** True when nothing solid stands between the two points. */
    function lineOfSight(map, ax, ay, bx, by, doorOpenness) {
        var dx = bx - ax, dy = by - ay;
        var d = Math.sqrt(dx * dx + dy * dy);
        if (d < 0.001) return true;
        return rayTrace(map, ax, ay, Math.atan2(dy, dx), d, doorOpenness) >= d - 0.001;
    }

    DOOM.Renderer = Renderer;
    DOOM.rayTrace = rayTrace;
    DOOM.lineOfSight = lineOfSight;
    DOOM.MAX_DEPTH = MAX_DEPTH;

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 08_weapons.js --- */

/* ==========================================================================
 * DOOM :: weapons.js -- the arsenal, its state machine and first-person art
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;
    var C = U.C, R = U.RAMP, T = U.TRANSPARENT;
    var Raster = DOOM.Raster;

    var AMMO = { NONE: null, BULLETS: 'bullets', SHELLS: 'shells', CELLS: 'cells' };
    var AMMO_MAX = { bullets: 200, shells: 50, cells: 300 };

    /**
     * Weapon table. `refire` is the delay between shots in seconds, `raise`
     * and `lower` are the switch animation times, and `spread` is the
     * half-angle in radians a hitscan pellet can wander from the crosshair.
     */
    var WEAPONS = [
        {
            id: 'fist', name: 'FIST', slot: 1, ammo: AMMO.NONE, use: 0,
            damage: [2, 20], pellets: 1, refire: 0.42, raise: 0.16, lower: 0.16,
            spread: 0, range: 1.6, melee: true, sound: 'punch', frames: 4,
            fireFrame: 2, flash: 0, kick: 0
        },
        {
            id: 'pistol', name: 'PISTOL', slot: 2, ammo: AMMO.BULLETS, use: 1,
            damage: [5, 15], pellets: 1, refire: 0.4, raise: 0.16, lower: 0.16,
            spread: 0.028, range: 32, sound: 'pistol', frames: 3,
            fireFrame: 1, flash: 0.09, kick: 3
        },
        {
            id: 'shotgun', name: 'SHOTGUN', slot: 3, ammo: AMMO.SHELLS, use: 1,
            damage: [5, 15], pellets: 7, refire: 0.9, raise: 0.2, lower: 0.2,
            spread: 0.085, range: 32, sound: 'shotgun', frames: 4,
            fireFrame: 1, flash: 0.13, kick: 9
        },
        {
            id: 'chaingun', name: 'CHAINGUN', slot: 4, ammo: AMMO.BULLETS, use: 1,
            damage: [5, 15], pellets: 1, refire: 0.11, raise: 0.18, lower: 0.18,
            spread: 0.05, range: 32, sound: 'chaingun', frames: 3,
            fireFrame: 1, flash: 0.07, kick: 2, spinUp: true
        },
        {
            id: 'plasma', name: 'PLASMA GUN', slot: 5, ammo: AMMO.CELLS, use: 1,
            damage: [5, 40], pellets: 1, refire: 0.1, raise: 0.2, lower: 0.2,
            spread: 0.012, range: 32, sound: 'plasma', frames: 3,
            fireFrame: 1, flash: 0.08, kick: 2, projectile: 'plasma'
        }
    ];

    function byId(id) {
        for (var i = 0; i < WEAPONS.length; i++) if (WEAPONS[i].id === id) return i;
        return -1;
    }

    // ------------------------------------------------------------ state machine

    var ST_READY = 'ready', ST_FIRE = 'fire', ST_LOWER = 'lower', ST_RAISE = 'raise';

    function makeState(index) {
        return {
            index: index === undefined ? 1 : index,
            pending: -1,
            state: ST_READY,
            t: 0,            // seconds spent in the current state
            frame: 0,        // animation frame index
            flash: 0,        // remaining muzzle-flash time
            offset: 0,       // vertical offset in pixels while switching
            kick: 0,         // recoil, decays back to zero
            spin: 0          // chaingun barrel rotation
        };
    }

    /**
     * Advance the weapon by `dt` seconds.
     *
     * `ctx` supplies the outside world:
     *   firing   -- is the trigger held this frame
     *   owned    -- array of booleans, one per weapon
     *   ammo     -- {bullets, shells, cells}
     *   want     -- index the player asked to switch to, or -1
     *   onFire(index) -- called once per shot; must consume the ammo
     *
     * Kept free of rendering and audio so the tests can drive it directly.
     */
    function update(ws, dt, ctx) {
        var wp = WEAPONS[ws.index];
        ws.t += dt;
        if (ws.flash > 0) ws.flash = Math.max(0, ws.flash - dt);
        ws.kick += (0 - ws.kick) * Math.min(1, dt * 9);

        // A switch request is honoured as soon as the weapon is idle.
        if (ctx.want >= 0 && ctx.want !== ws.index && ctx.owned[ctx.want] &&
            (ws.state === ST_READY || ws.state === ST_FIRE)) {
            if (ws.state === ST_READY) {
                ws.state = ST_LOWER;
                ws.t = 0;
                ws.pending = ctx.want;
            } else {
                ws.pending = ctx.want;
            }
        }

        if (wp.spinUp) {
            var target = (ctx.firing && ws.state !== ST_LOWER && ws.state !== ST_RAISE) ? 1 : 0;
            ws.spin += (target - ws.spin) * Math.min(1, dt * 6);
        } else {
            ws.spin = 0;
        }

        switch (ws.state) {
            case ST_LOWER:
                ws.offset = Math.min(1, ws.t / wp.lower);
                if (ws.t >= wp.lower) {
                    ws.index = ws.pending >= 0 ? ws.pending : ws.index;
                    ws.pending = -1;
                    ws.state = ST_RAISE;
                    ws.t = 0;
                    ws.frame = 0;
                }
                break;

            case ST_RAISE:
                ws.offset = 1 - Math.min(1, ws.t / WEAPONS[ws.index].raise);
                if (ws.t >= WEAPONS[ws.index].raise) {
                    ws.offset = 0;
                    ws.state = ST_READY;
                    ws.t = 0;
                }
                break;

            case ST_FIRE:
                ws.offset = 0;
                // walk the firing animation, then either refire or go idle
                var span = wp.refire;
                var f = Math.min(wp.frames - 1, 1 + Math.floor((ws.t / span) * (wp.frames - 1)));
                ws.frame = f;
                if (ws.t >= span) {
                    if (ws.pending >= 0) {
                        ws.state = ST_LOWER;
                        ws.t = 0;
                    } else if (ctx.firing && canFire(ws.index, ctx)) {
                        fire(ws, ctx);
                    } else {
                        ws.state = ST_READY;
                        ws.t = 0;
                        ws.frame = 0;
                    }
                }
                break;

            default: // ST_READY
                ws.offset = 0;
                ws.frame = 0;
                if (ctx.firing && canFire(ws.index, ctx)) {
                    fire(ws, ctx);
                } else if (ctx.firing && !canFire(ws.index, ctx) && ctx.onEmpty) {
                    ctx.onEmpty(ws.index);
                }
                break;
        }
        return ws;
    }

    function canFire(index, ctx) {
        var wp = WEAPONS[index];
        if (!wp.ammo) return true;
        return (ctx.ammo[wp.ammo] || 0) >= wp.use;
    }

    function fire(ws, ctx) {
        var wp = WEAPONS[ws.index];
        ws.state = ST_FIRE;
        ws.t = 0;
        ws.frame = wp.fireFrame;
        ws.flash = wp.flash;
        ws.kick = wp.kick;
        if (ctx.onFire) ctx.onFire(ws.index);
    }

    /** Pick the best weapon the player owns and can feed -- used after a death or pickup. */
    function bestAvailable(owned, ammo) {
        for (var i = WEAPONS.length - 1; i >= 0; i--) {
            if (!owned[i]) continue;
            var wp = WEAPONS[i];
            if (!wp.ammo || (ammo[wp.ammo] || 0) >= wp.use) return i;
        }
        return 0;
    }

    // ------------------------------------------------------- first-person art

    var VW = 240, VH = 170;      // weapon sprite canvas, in render-buffer pixels

    function gloveHand(r, cx, cy, rr) {
        r.ellipse(cx, cy, rr, rr * 0.85, C(R.BROWN, 6));
        r.ellipse(cx, cy - rr * 0.3, rr * 0.9, rr * 0.4, C(R.BROWN, 8));
        for (var i = -1; i <= 2; i++) {
            r.ellipse(cx + i * rr * 0.45, cy + rr * 0.45, rr * 0.22, rr * 0.4, C(R.BROWN, 5));
        }
        r.rect(cx - rr, cy + rr * 0.6, rr * 2, rr * 0.5, C(R.GREEN, 5));
    }

    function drawFist(frame) {
        var r = new Raster(VW, VH, T);
        var punch = [0, 0.4, 1, 0.5][frame] || 0;
        var cx = VW * 0.66 - punch * 30;
        var cy = VH - 40 - punch * 46;
        // forearm running off the bottom of the screen
        var ax = VW * 0.86, ay = VH + 12;
        for (var i = 0; i <= 20; i++) {
            var t = i / 20;
            r.ellipse(U.lerp(ax, cx, t), U.lerp(ay, cy, t), 20 - t * 4, 18 - t * 3, C(R.GREEN, 5 + (i & 1)));
        }
        r.ellipse(cx, cy, 30, 27, C(R.BROWN, 6));
        r.ellipse(cx, cy - 8, 27, 12, C(R.BROWN, 8));
        for (var k = 0; k < 4; k++) {
            r.ellipse(cx - 18 + k * 12, cy + 10, 6, 9, C(R.BROWN, 4 + (k & 1)));
            r.ellipse(cx - 18 + k * 12, cy + 4, 5, 4, C(R.BROWN, 9));
        }
        r.ellipse(cx, cy + 22, 26, 7, C(R.GREEN, 4));
        return r;
    }

    function drawPistol(frame) {
        var r = new Raster(VW, VH, T);
        var recoil = frame === 1 ? 16 : (frame === 2 ? 6 : 0);
        var bx = VW * 0.5, by = VH - 52 + recoil;

        // slide and frame
        r.rect(bx - 9, by - 46, 20, 44, C(R.STEEL, 4));
        r.rect(bx - 9, by - 46, 20, 4, C(R.STEEL, 8));
        r.rect(bx - 6, by - 46, 3, 44, C(R.STEEL, 7));
        r.rect(bx - 4, by - 52, 12, 8, C(R.STEEL, 3));      // barrel shroud
        r.rect(bx - 2, by - 56, 7, 5, C(R.STEEL, 2));
        r.rect(bx - 13, by - 6, 28, 12, C(R.STEEL, 5));     // frame
        r.rect(bx - 11, by + 4, 22, 26, C(R.BROWN, 5));     // grip
        r.hgrad(bx - 11, by + 4, 22, 26, R.BROWN, 7, 3);

        // gloved hands wrapped around it
        gloveHand(r, bx + 20, by + 16, 22);
        gloveHand(r, bx - 20, by + 22, 20);
        r.ellipse(bx, by + 12, 16, 12, C(R.BROWN, 7));

        // arms
        for (var i = 0; i <= 14; i++) {
            var t = i / 14;
            r.ellipse(U.lerp(bx + 46, bx + 22, t), U.lerp(VH + 16, by + 22, t), 17, 15, C(R.GREEN, 5));
            r.ellipse(U.lerp(bx - 46, bx - 22, t), U.lerp(VH + 16, by + 28, t), 16, 14, C(R.GREEN, 4));
        }

        if (frame === 1) {
            var fx = bx + 1, fy = by - 62;
            r.circle(fx, fy, 20, C(R.FIRE, 12));
            r.circle(fx, fy, 13, C(R.YELLOW, 15));
            r.circle(fx, fy - 4, 7, C(R.BONE, 15));
            for (var s = 0; s < 8; s++) {
                var a = (s / 8) * U.PI2;
                r.circle(fx + Math.cos(a) * 24, fy + Math.sin(a) * 17, 4, C(R.FIRE, 11));
            }
        }
        return r;
    }

    function drawShotgun(frame) {
        var r = new Raster(VW, VH, T);
        // frame 0 idle, 1 fire, 2 pump back, 3 pump forward
        var recoil = frame === 1 ? 20 : 0;
        var pump = frame === 2 ? 20 : (frame === 3 ? 8 : 0);
        var bx = VW * 0.5, by = VH - 30 + recoil;

        // receiver + barrel, angled up to the left like the original
        r.poly([[bx - 16, by], [bx + 30, by], [bx + 14, by - 92], [bx - 4, by - 92]], C(R.STEEL, 4));
        r.poly([[bx - 10, by - 10], [bx + 2, by - 10], [bx - 2, by - 92], [bx - 8, by - 92]], C(R.STEEL, 7));
        r.rect(bx - 6, by - 100, 16, 10, C(R.STEEL, 2));
        r.ellipse(bx + 2, by - 100, 8, 4, C(R.STEEL, 1));

        // wooden pump, slid back on frame 2
        r.rect(bx - 2, by - 62 + pump, 22, 22, C(R.BROWN, 5));
        r.hgrad(bx - 2, by - 62 + pump, 22, 22, R.BROWN, 8, 3);
        for (var g = 0; g < 4; g++) r.rect(bx - 2, by - 58 + pump + g * 5, 22, 1, C(R.BROWN, 2));

        // stock and hands
        r.poly([[bx + 18, by - 14], [bx + 62, by + 30], [bx + 74, by + 16], [bx + 30, by - 24]], C(R.BROWN, 4));
        gloveHand(r, bx + 10, by - 52 + pump, 20);
        gloveHand(r, bx + 26, by + 6, 21);
        for (var i = 0; i <= 12; i++) {
            var t = i / 12;
            r.ellipse(U.lerp(bx + 60, bx + 30, t), U.lerp(VH + 20, by + 10, t), 17, 15, C(R.GREEN, 5));
            r.ellipse(U.lerp(bx - 30, bx + 6, t), U.lerp(VH + 20, by - 44 + pump, t), 16, 14, C(R.GREEN, 4));
        }

        if (frame === 1) {
            var fx = bx + 2, fy = by - 112;
            r.circle(fx, fy, 32, C(R.FIRE, 11));
            r.circle(fx, fy, 21, C(R.FIRE, 14));
            r.circle(fx, fy - 5, 12, C(R.YELLOW, 15));
            for (var s = 0; s < 12; s++) {
                var a = (s / 12) * U.PI2;
                r.circle(fx + Math.cos(a) * 38, fy + Math.sin(a) * 26, 6, C(R.FIRE, 10));
            }
        }
        if (frame === 2) {
            // ejected shell tumbling away
            r.rect(bx + 34, by - 46, 7, 12, C(R.RED, 9));
            r.rect(bx + 34, by - 36, 7, 4, C(R.YELLOW, 11));
        }
        return r;
    }

    function drawChaingun(frame, spinPhase) {
        var r = new Raster(VW, VH, T);
        var recoil = frame > 0 ? 8 : 0;
        var bx = VW * 0.5, by = VH - 34 + recoil;

        // rotating barrel cluster
        var barrels = 6;
        for (var i = 0; i < barrels; i++) {
            var a = (i / barrels) * U.PI2 + spinPhase;
            var ox = Math.cos(a) * 17;
            var depth = Math.sin(a);                 // fake perspective on the ring
            var shade = 3 + Math.round((depth + 1) * 3);
            r.rect(bx + ox - 5, by - 96 - depth * 3, 10, 62, C(R.STEEL, shade));
            r.ellipse(bx + ox, by - 96 - depth * 3, 5, 3, C(R.STEEL, shade + 3));
        }
        // housing
        r.rect(bx - 26, by - 40, 52, 34, C(R.STEEL, 4));
        r.frame(bx - 26, by - 40, 52, 34, C(R.STEEL, 7), 2);
        r.rect(bx - 22, by - 34, 44, 6, C(R.STEEL, 2));
        r.circle(bx, by - 22, 12, C(R.STEEL, 6));
        r.circle(bx, by - 22, 6, C(R.STEEL, 2));
        // ammo belt feeding in from the left
        for (var b = 0; b < 7; b++) {
            r.rect(bx - 30 - b * 9, by - 18 + b * 3, 8, 12, C(R.YELLOW, 9));
            r.rect(bx - 30 - b * 9, by - 18 + b * 3, 8, 3, C(R.BROWN, 5));
        }
        r.rect(bx - 10, by - 6, 22, 26, C(R.BROWN, 5));
        gloveHand(r, bx + 18, by + 12, 21);
        gloveHand(r, bx - 20, by + 16, 20);
        for (var k = 0; k <= 12; k++) {
            var t = k / 12;
            r.ellipse(U.lerp(bx + 56, bx + 24, t), U.lerp(VH + 20, by + 14, t), 17, 15, C(R.GREEN, 5));
            r.ellipse(U.lerp(bx - 58, bx - 24, t), U.lerp(VH + 20, by + 20, t), 16, 14, C(R.GREEN, 4));
        }
        if (frame > 0) {
            var fy = by - 106;
            r.circle(bx, fy, 24, C(R.FIRE, 12));
            r.circle(bx, fy, 15, C(R.YELLOW, 15));
            r.circle(bx + (frame === 1 ? -8 : 8), fy - 6, 8, C(R.FIRE, 13));
        }
        return r;
    }

    function drawPlasma(frame) {
        var r = new Raster(VW, VH, T);
        var recoil = frame > 0 ? 6 : 0;
        var bx = VW * 0.5, by = VH - 30 + recoil;

        r.rect(bx - 30, by - 66, 60, 46, C(R.STEEL, 4));
        r.frame(bx - 30, by - 66, 60, 46, C(R.STEEL, 8), 2);
        r.rect(bx - 24, by - 60, 48, 16, C(R.PLASMA, frame > 0 ? 15 : 10));
        r.rect(bx - 24, by - 40, 48, 8, C(R.STEEL, 2));
        // coil emitter
        r.rect(bx - 12, by - 88, 24, 24, C(R.STEEL, 3));
        for (var c = 0; c < 4; c++) {
            r.rect(bx - 16, by - 86 + c * 6, 32, 3, C(R.STEEL, 7));
        }
        r.circle(bx, by - 92, 11, C(R.PLASMA, frame > 0 ? 15 : 8));
        r.circle(bx, by - 92, 6, C(R.BONE, frame > 0 ? 15 : 10));

        r.rect(bx - 10, by - 20, 22, 26, C(R.BROWN, 5));
        gloveHand(r, bx + 20, by + 4, 21);
        gloveHand(r, bx - 22, by + 8, 20);
        for (var i = 0; i <= 12; i++) {
            var t = i / 12;
            r.ellipse(U.lerp(bx + 56, bx + 26, t), U.lerp(VH + 20, by + 6, t), 17, 15, C(R.GREEN, 5));
            r.ellipse(U.lerp(bx - 56, bx - 26, t), U.lerp(VH + 20, by + 12, t), 16, 14, C(R.GREEN, 4));
        }
        if (frame > 0) {
            r.circle(bx, by - 106, 20, C(R.PLASMA, 14));
            r.circle(bx, by - 108, 11, C(R.BONE, 15));
            for (var s = 0; s < 6; s++) {
                var a = (s / 6) * U.PI2 + frame;
                r.circle(bx + Math.cos(a) * 26, by - 106 + Math.sin(a) * 16, 4, C(R.PLASMA, 12));
            }
        }
        return r;
    }

    var builtViews = null;

    /** Build every first-person frame once; chaingun spin frames included. */
    function buildViews() {
        if (builtViews) return builtViews;
        var v = {};
        var i;
        v.fist = [];
        for (i = 0; i < 4; i++) v.fist.push(drawFist(i));
        v.pistol = [];
        for (i = 0; i < 3; i++) v.pistol.push(drawPistol(i));
        v.shotgun = [];
        for (i = 0; i < 4; i++) v.shotgun.push(drawShotgun(i));
        v.chaingun = [];
        // three animation frames x four spin phases
        for (i = 0; i < 3; i++) {
            var row = [];
            for (var s = 0; s < 4; s++) row.push(drawChaingun(i, (s / 4) * U.PI2 / 6));
            v.chaingun.push(row);
        }
        v.plasma = [];
        for (i = 0; i < 3; i++) v.plasma.push(drawPlasma(i));
        builtViews = v;
        return v;
    }

    /** Pick the raster for the current weapon state. */
    function viewFor(ws) {
        var views = buildViews();
        var wp = WEAPONS[ws.index];
        var set = views[wp.id];
        var f = U.clamp(ws.frame, 0, wp.frames - 1) | 0;
        if (wp.id === 'chaingun') {
            var phase = (ws.spinFrame || 0) & 3;
            return set[Math.min(f, set.length - 1)][phase];
        }
        return set[Math.min(f, set.length - 1)];
    }

    DOOM.Weapons = {
        WEAPONS: WEAPONS,
        AMMO: AMMO,
        AMMO_MAX: AMMO_MAX,
        VW: VW, VH: VH,
        ST_READY: ST_READY, ST_FIRE: ST_FIRE, ST_LOWER: ST_LOWER, ST_RAISE: ST_RAISE,
        byId: byId,
        makeState: makeState,
        update: update,
        canFire: canFire,
        bestAvailable: bestAvailable,
        buildViews: buildViews,
        viewFor: viewFor
    };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 09_monsters.js --- */

/* ==========================================================================
 * DOOM :: monsters.js -- bestiary and the idle/chase/attack/pain/death AI
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;

    var S_IDLE = 'idle', S_CHASE = 'chase', S_ATTACK = 'attack',
        S_PAIN = 'pain', S_DEATH = 'death', S_GONE = 'gone';

    /**
     * Bestiary. Health and pain chances follow the original where it matters;
     * speeds are in map units per second.
     *
     *   painChance -- 0..1 probability a hit interrupts the monster
     *   worldH     -- billboard height in wall-heights
     *   gibHealth  -- corpse explodes if health falls below this (negative)
     */
    var MONSTERS = {
        zombieman: {
            id: 'zombieman', name: 'Former Human', sprite: 'zombieman',
            hp: 20, radius: 0.3, speed: 1.5, worldH: 0.66, gibHealth: -20,
            sightRange: 22, painChance: 0.78, meleeRange: 0, attackRange: 24,
            attackDelay: 1.1, damage: [3, 15], pellets: 1, spread: 0.09,
            kind: 'hitscan', sightSound: 'sightZombie', pitch: 1.0,
            dropItem: 'clip', score: 1
        },
        sergeant: {
            id: 'sergeant', name: 'Former Sergeant', sprite: 'sergeant',
            hp: 30, radius: 0.3, speed: 1.6, worldH: 0.68, gibHealth: -30,
            sightRange: 22, painChance: 0.67, meleeRange: 0, attackRange: 22,
            attackDelay: 1.4, damage: [3, 15], pellets: 3, spread: 0.14,
            kind: 'hitscan', sightSound: 'sightZombie', pitch: 0.88,
            dropItem: 'shotgun', score: 1
        },
        imp: {
            id: 'imp', name: 'Imp', sprite: 'imp',
            hp: 60, radius: 0.32, speed: 1.7, worldH: 0.74, gibHealth: -60,
            sightRange: 26, painChance: 0.78, meleeRange: 1.3, attackRange: 28,
            attackDelay: 1.3, damage: [3, 24], meleeDamage: [3, 24],
            kind: 'projectile', projectile: 'fireball',
            sightSound: 'sightImp', pitch: 1.15, score: 1
        },
        demon: {
            id: 'demon', name: 'Demon', sprite: 'demon',
            hp: 150, radius: 0.42, speed: 3.0, worldH: 0.62, gibHealth: -150,
            sightRange: 24, painChance: 0.71, meleeRange: 1.5, attackRange: 0,
            attackDelay: 0.9, meleeDamage: [4, 40],
            kind: 'melee', sightSound: 'sightDemon', pitch: 0.7, score: 1
        },
        baron: {
            id: 'baron', name: 'Baron of Hell', sprite: 'baron',
            hp: 1000, radius: 0.5, speed: 1.6, worldH: 1.05, gibHealth: -1000,
            sightRange: 32, painChance: 0.19, meleeRange: 1.8, attackRange: 32,
            attackDelay: 1.6, damage: [8, 64], meleeDamage: [10, 80],
            kind: 'projectile', projectile: 'baronball',
            sightSound: 'sightBaron', pitch: 0.55, score: 1
        },
        cyberdemon: {
            id: 'cyberdemon', name: 'Cyberdemon', sprite: 'cyberdemon',
            hp: 4000, radius: 0.6, speed: 1.5, worldH: 1.65, gibHealth: -4000,
            sightRange: 40, painChance: 0, meleeRange: 0, attackRange: 40,
            attackDelay: 2.2, damage: [20, 128], burst: 3, burstDelay: 0.32,
            kind: 'projectile', projectile: 'rocket', noPain: true,
            sightSound: 'sightCyber', pitch: 0.45, footstep: 'hoof',
            boss: true, score: 1
        }
    };

    /** Skill modifiers, matching Doom's four difficulty levels. */
    var SKILLS = [
        { id: 0, name: "I'M TOO YOUNG TO DIE", damageTaken: 0.5, speed: 0.85, aggression: 0.7, ammoBonus: 2, minThing: 1 },
        { id: 1, name: 'HURT ME PLENTY', damageTaken: 1.0, speed: 1.0, aggression: 1.0, ammoBonus: 1, minThing: 2 },
        { id: 2, name: 'ULTRA-VIOLENCE', damageTaken: 1.0, speed: 1.15, aggression: 1.35, ammoBonus: 1, minThing: 3 },
        { id: 3, name: 'NIGHTMARE!', damageTaken: 1.0, speed: 1.45, aggression: 2.0, ammoBonus: 1, minThing: 3, respawn: true }
    ];

    function spawn(typeId, x, y) {
        var def = MONSTERS[typeId];
        if (!def) throw new Error('unknown monster type: ' + typeId);
        return {
            type: typeId,
            def: def,
            x: x, y: y,
            ang: 0,
            hp: def.hp,
            maxHp: def.hp,
            state: S_IDLE,
            stateT: 0,
            frame: 0,
            animT: 0,
            attackCooldown: 0,
            burstLeft: 0,
            alerted: false,
            deathFrame: 0,
            gibbed: false,
            strafe: 0,
            strafeT: 0,
            radius: def.radius,
            counted: false
        };
    }

    /**
     * One AI tick.
     *
     * `w` (the world adaptor) must provide:
     *   player {x, y, dead}
     *   dt, skill
     *   canSee(m)              -- line of sight test
     *   tryMove(m, dx, dy)     -- collision-resolved movement
     *   hitscan(m, dmg, spread, pellets)
     *   launch(m, projectileId)
     *   meleeHit(m, dmg)
     *   sound(name, m, pitch)
     *   onDeath(m)
     */
    function update(m, w) {
        var dt = w.dt;
        var def = m.def;
        m.stateT += dt;
        m.animT += dt;
        if (m.attackCooldown > 0) m.attackCooldown -= dt;

        if (m.state === S_DEATH) {
            // step through the death animation, then rest as a corpse
            var frames = 6;
            var per = 0.09;
            var f = Math.min(frames - 1, Math.floor(m.stateT / per));
            m.deathFrame = f;
            if (m.stateT > per * frames + 0.05) m.settled = true;
            return;
        }
        if (m.state === S_GONE) return;

        var px = w.player.x, py = w.player.y;
        var dx = px - m.x, dy = py - m.y;
        var dist = Math.sqrt(dx * dx + dy * dy);
        var sees = dist <= def.sightRange && w.canSee(m);

        if (m.state === S_PAIN) {
            if (m.stateT > 0.28) { m.state = S_CHASE; m.stateT = 0; }
            return;
        }

        if (m.state === S_IDLE) {
            if (sees && !w.player.dead) {
                m.alerted = true;
                m.state = S_CHASE;
                m.stateT = 0;
                if (def.sightSound) w.sound(def.sightSound, m, def.pitch);
            }
            return;
        }

        // Once alerted a monster keeps hunting even when it loses sight.
        m.ang = Math.atan2(dy, dx);

        if (m.state === S_ATTACK) {
            var windup = 0.34;
            m.frame = m.stateT < windup ? 0 : 1;
            if (m.stateT >= windup && !m.fired) {
                m.fired = true;
                performAttack(m, w, dist);
            }
            if (m.stateT >= windup + 0.3) {
                if (m.burstLeft > 0 && sees) {
                    m.burstLeft--;
                    m.stateT = windup - (def.burstDelay || 0.3);
                    m.fired = false;
                } else {
                    m.state = S_CHASE;
                    m.stateT = 0;
                    m.fired = false;
                    m.attackCooldown = def.attackDelay / (w.skill.aggression || 1);
                }
            }
            return;
        }

        // ---- chase
        var speed = def.speed * (w.skill.speed || 1);
        var canMelee = def.meleeRange > 0 && dist <= def.meleeRange;
        var canShoot = def.attackRange > 0 && dist <= def.attackRange && sees;

        if (!w.player.dead && m.attackCooldown <= 0 && (canMelee || canShoot)) {
            m.state = S_ATTACK;
            m.stateT = 0;
            m.fired = false;
            m.meleeThisAttack = canMelee;
            m.burstLeft = (!canMelee && def.burst) ? def.burst - 1 : 0;
            return;
        }

        // Walk towards the player, weaving a little so a group does not stack
        // into a single-file conga line.
        m.strafeT -= dt;
        if (m.strafeT <= 0) {
            m.strafeT = 0.6 + Math.random() * 1.2;
            m.strafe = (Math.random() - 0.5) * 1.4;
        }
        var moveAng = m.ang + m.strafe * 0.6;
        var vx = Math.cos(moveAng) * speed * dt;
        var vy = Math.sin(moveAng) * speed * dt;
        var moved = w.tryMove(m, vx, vy);
        if (!moved) {
            // blocked: sidestep rather than grind against the wall
            m.strafe = (Math.random() - 0.5) * 3;
            m.strafeT = 0.4;
        }

        // walk cycle
        var cycle = 0.16 / Math.max(0.3, speed / def.speed);
        m.frame = Math.floor(m.animT / cycle) & 3;
        if (def.footstep && Math.floor(m.animT / cycle) !== m.lastStep) {
            m.lastStep = Math.floor(m.animT / cycle);
            if ((m.lastStep & 1) === 0) w.sound(def.footstep, m, 1);
        }
    }

    function performAttack(m, w, dist) {
        var def = m.def;
        if (m.meleeThisAttack) {
            w.meleeHit(m, def.meleeDamage || def.damage);
            w.sound(def.id === 'demon' ? 'fleshhit' : 'punch', m, 1);
            return;
        }
        if (def.kind === 'hitscan') {
            w.sound(def.pellets > 1 ? 'shotgun' : 'pistol', m, 1);
            w.hitscan(m, def.damage, def.spread, def.pellets || 1);
        } else if (def.kind === 'projectile') {
            w.sound('fireball', m, 1);
            w.launch(m, def.projectile);
        } else {
            w.meleeHit(m, def.meleeDamage || def.damage);
        }
    }

    /**
     * Apply damage. Returns 'dead', 'pain' or 'hurt'.
     * Waking a monster by shooting it is deliberate: it is how Doom plays.
     */
    function damage(m, amount, w) {
        if (m.state === S_DEATH || m.state === S_GONE) return 'dead';
        m.hp -= amount;
        m.alerted = true;
        if (m.state === S_IDLE) { m.state = S_CHASE; m.stateT = 0; }

        if (m.hp <= 0) {
            m.state = S_DEATH;
            m.stateT = 0;
            m.deathFrame = 0;
            m.gibbed = m.hp <= m.def.gibHealth && !!(DOOM.Sprites && m.def.sprite);
            if (w) {
                w.sound(m.gibbed ? 'gib' : 'monsterDeath', m, m.def.pitch);
                if (w.onDeath) w.onDeath(m);
            }
            return 'dead';
        }
        if (!m.def.noPain && Math.random() < m.def.painChance) {
            m.state = S_PAIN;
            m.stateT = 0;
            if (w) w.sound('monsterPain', m, m.def.pitch);
            return 'pain';
        }
        return 'hurt';
    }

    /**
     * Choose the sprite view (0 front .. 3 back) for a monster as seen from
     * `viewAng`, plus whether it should be drawn mirrored.
     */
    function viewIndex(monsterAng, viewX, viewY, mx, my) {
        var toViewer = Math.atan2(viewY - my, viewX - mx);
        var rel = U.angleDiff(toViewer, monsterAng);   // -PI..PI
        var a = Math.abs(rel);
        var idx;
        if (a < Math.PI / 8) idx = 0;
        else if (a < 3 * Math.PI / 8) idx = 1;
        else if (a < 5 * Math.PI / 8) idx = 2;
        else if (a < 7 * Math.PI / 8) idx = 3;
        else idx = 3;
        return { view: idx, flip: rel < 0 };
    }

    /** Resolve the current animation raster for a monster. */
    function frameFor(m, sprites, viewIdx) {
        var set = sprites.monsters[m.def.sprite];
        if (!set) return null;
        switch (m.state) {
            case S_DEATH:
                var arr = (m.gibbed && set.gib) ? set.gib : set.death;
                return arr[Math.min(arr.length - 1, m.deathFrame)];
            case S_PAIN:
                return set.pain[viewIdx];
            case S_ATTACK:
                return set.attack[viewIdx][Math.min(1, m.frame)];
            default:
                return set.walk[viewIdx][m.frame & 3];
        }
    }

    DOOM.Monsters = {
        MONSTERS: MONSTERS,
        SKILLS: SKILLS,
        S_IDLE: S_IDLE, S_CHASE: S_CHASE, S_ATTACK: S_ATTACK,
        S_PAIN: S_PAIN, S_DEATH: S_DEATH, S_GONE: S_GONE,
        spawn: spawn,
        update: update,
        damage: damage,
        viewIndex: viewIndex,
        frameFor: frameFor
    };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 10_hud.js --- */

/* ==========================================================================
 * DOOM :: hud.js -- bitmap font, status bar and the Doomguy face widget
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;
    var C = U.C, R = U.RAMP, T = U.TRANSPARENT;
    var Raster = DOOM.Raster;
    var W = DOOM.Weapons;

    // ------------------------------------------------------------------- font
    // 5x7 glyphs, one 35-character string each. Drives the status bar, the
    // menus, the intermission and every message in the game.

    var GLYPHS = {
        '0': '01110100011001110101110011000101110',
        '1': '00100011000010000100001000010001110',
        '2': '01110100010000100010001000100011111',
        '3': '11111000100010000010000110001011100',
        '4': '00010001100101010010111110001000010',
        '5': '11111100001111000001000011000101110',
        '6': '00110010001000011110100011000101110',
        '7': '11111000010001000100010000100001000',
        '8': '01110100011000101110100011000101110',
        '9': '01110100011000101111000010001001100',
        'A': '01110100011000111111100011000110001',
        'B': '11110100011000111110100011000111110',
        'C': '01110100011000010000100001000101110',
        'D': '11100100101000110001100011001011100',
        'E': '11111100001000011110100001000011111',
        'F': '11111100001000011110100001000010000',
        'G': '01110100011000010111100011000101111',
        'H': '10001100011000111111100011000110001',
        'I': '01110001000010000100001000010001110',
        'J': '00111000100001000010000101001001100',
        'K': '10001100101010011000101001001010001',
        'L': '10000100001000010000100001000011111',
        'M': '10001110111010110101100011000110001',
        'N': '10001110011010110011100011000110001',
        'O': '01110100011000110001100011000101110',
        'P': '11110100011000111110100001000010000',
        'Q': '01110100011000110001101011001001101',
        'R': '11110100011000111110101001001010001',
        'S': '01111100001000001110000010000111110',
        'T': '11111001000010000100001000010000100',
        'U': '10001100011000110001100011000101110',
        'V': '10001100011000110001100010101000100',
        'W': '10001100011000110101101011101110001',
        'X': '10001100010101000100010101000110001',
        'Y': '10001100010101000100001000010000100',
        'Z': '11111000010001000100010001000011111',
        '%': '11001110100001000100010000101110011',
        '-': '00000000000000011111000000000000000',
        '.': '00000000000000000000000001100011000',
        ',': '00000000000000000000001100001000010',
        ':': '00000011000110000000011000110000000',
        '!': '00100001000010000100001000000000100',
        '?': '01110100010000100110001000000000100',
        "'": '00100001000010000000000000000000000',
        '/': '00001000100001000100010000100010000',
        '(': '00010001000100001000010000010000010',
        ')': '01000001000001000010000100010001000',
        '*': '00000101010111011111011101010100000',
        '+': '00000001000010011111001000010000000',
        '=': '00000000001111100000111110000000000',
        '<': '00010001000100010000010000010000010',
        '>': '01000001000001000001000100010001000',
        '#': '01010010101111101010111110101001010',
        ' ': '00000000000000000000000000000000000'
    };

    var GLYPH_W = 5, GLYPH_H = 7;

    function glyph(ch) {
        return GLYPHS[ch] || GLYPHS[ch.toUpperCase()] || GLYPHS['?'];
    }

    /** Text width in pixels at the given scale (1px letter spacing). */
    function textWidth(text, scale) {
        if (!text.length) return 0;
        return (text.length * (GLYPH_W + 1) - 1) * scale;
    }

    /** Draw text into an indexed raster. */
    function drawText(r, text, x, y, scale, color, shadow) {
        scale = scale || 1;
        var cx = x;
        for (var i = 0; i < text.length; i++) {
            var g = glyph(text.charAt(i));
            for (var gy = 0; gy < GLYPH_H; gy++) {
                for (var gx = 0; gx < GLYPH_W; gx++) {
                    if (g.charAt(gy * GLYPH_W + gx) !== '1') continue;
                    if (shadow !== undefined) {
                        r.rect(cx + gx * scale + scale, y + gy * scale + scale, scale, scale, shadow);
                    }
                    r.rect(cx + gx * scale, y + gy * scale, scale, scale, color);
                }
            }
            cx += (GLYPH_W + 1) * scale;
        }
        return cx;
    }

    /** Draw text straight onto a 2D canvas -- used by menus and overlays. */
    function drawTextCanvas(ctx, text, x, y, scale, cssColor, shadowCss) {
        var cx = x;
        for (var i = 0; i < text.length; i++) {
            var g = glyph(text.charAt(i));
            for (var gy = 0; gy < GLYPH_H; gy++) {
                for (var gx = 0; gx < GLYPH_W; gx++) {
                    if (g.charAt(gy * GLYPH_W + gx) !== '1') continue;
                    if (shadowCss) {
                        ctx.fillStyle = shadowCss;
                        ctx.fillRect(cx + gx * scale + scale, y + gy * scale + scale, scale, scale);
                    }
                    ctx.fillStyle = cssColor;
                    ctx.fillRect(cx + gx * scale, y + gy * scale, scale, scale);
                }
            }
            cx += (GLYPH_W + 1) * scale;
        }
        return cx;
    }

    function drawTextCentered(ctx, text, cx, y, scale, cssColor, shadowCss) {
        return drawTextCanvas(ctx, text, cx - textWidth(text, scale) / 2, y, scale, cssColor, shadowCss);
    }

    // ------------------------------------------------------------------- face
    //
    // The Doomguy face: five injury levels x three look directions, plus the
    // ouch, evil-grin, god and dead specials.

    var FACE_W = 30, FACE_H = 32;

    /**
     * @param {object} o {pain:0..4, dir:-1|0|1, mode:'normal'|'ouch'|'evil'|'god'|'dead'}
     */
    function drawFace(o) {
        var r = new Raster(FACE_W, FACE_H, T);
        var pain = U.clamp(o.pain | 0, 0, 4);
        var dir = o.dir || 0;
        var mode = o.mode || 'normal';
        var cx = FACE_W / 2, cy = FACE_H / 2;

        // recessed steel bezel
        r.rect(0, 0, FACE_W, FACE_H, C(R.STEEL, 2));
        r.frame(0, 0, FACE_W, FACE_H, C(R.STEEL, 5), 1);

        if (mode === 'dead') {
            // slack, bloodied, eyes rolled back
            r.ellipse(cx, cy + 4, 11, 9, C(R.FLESH, 4));
            r.ellipse(cx, cy - 3, 11, 6, C(R.BLOOD, 4));
            r.rect(cx - 9, cy - 6, 18, 3, C(R.BLOOD, 6));
            r.ellipse(cx - 4, cy + 3, 2.4, 1.6, C(R.BONE, 12));
            r.ellipse(cx + 4, cy + 3, 2.4, 1.6, C(R.BONE, 12));
            r.line(cx - 6, cy + 1, cx - 2, cy + 5, C(R.BLOOD, 1));
            r.line(cx + 6, cy + 1, cx + 2, cy + 5, C(R.BLOOD, 1));
            r.ellipse(cx, cy + 9, 5, 2.5, C(R.BLOOD, 1));
            for (var d = 0; d < 12; d++) {
                r.circle(cx - 10 + d * 2, cy + 11 + (d % 3), 1.4, C(R.BLOOD, 3 + (d % 4)));
            }
            return r;
        }

        var headShade = 8 - pain;
        // hair, head, jaw
        r.ellipse(cx, cy + 1, 10, 12, C(R.FLESH, headShade));
        r.ellipse(cx, cy + 9, 8, 5, C(R.FLESH, headShade - 1));
        r.ellipse(cx, cy - 8, 11, 6, C(R.BROWN, 4));
        r.rect(cx - 11, cy - 9, 22, 3, C(R.BROWN, 3));
        // sideburns
        r.rect(cx - 10, cy - 5, 2, 7, C(R.BROWN, 3));
        r.rect(cx + 8, cy - 5, 2, 7, C(R.BROWN, 3));

        // eyes, offset by the look direction
        var ex = dir * 2;
        var browY = cy - 4;
        if (mode === 'god') {
            r.ellipse(cx - 4, cy - 1, 3, 2.6, C(R.YELLOW, 15));
            r.ellipse(cx + 4, cy - 1, 3, 2.6, C(R.YELLOW, 15));
        } else {
            r.ellipse(cx - 4, cy - 1, 3, 2.6, C(R.BONE, 13));
            r.ellipse(cx + 4, cy - 1, 3, 2.6, C(R.BONE, 13));
            r.ellipse(cx - 4 + ex, cy - 1, 1.5, 1.8, C(R.STEEL, 1));
            r.ellipse(cx + 4 + ex, cy - 1, 1.5, 1.8, C(R.STEEL, 1));
        }
        // brows: angrier the more hurt he is
        var tilt = mode === 'ouch' ? -1 : 1;
        r.line(cx - 8, browY - tilt, cx - 1, browY + tilt, C(R.BROWN, 2));
        r.line(cx + 8, browY - tilt, cx + 1, browY + tilt, C(R.BROWN, 2));
        r.line(cx - 8, browY - tilt + 1, cx - 1, browY + tilt + 1, C(R.BROWN, 2));
        r.line(cx + 8, browY - tilt + 1, cx + 1, browY + tilt + 1, C(R.BROWN, 2));

        // nose and mouth
        r.rect(cx - 1, cy + 1, 2, 4, C(R.FLESH, headShade - 2));
        if (mode === 'evil') {
            // the grin you get for picking up a new gun
            r.ellipse(cx, cy + 7, 6, 2.6, C(R.BLOOD, 1));
            r.rect(cx - 5, cy + 5, 10, 2, C(R.BONE, 14));
            for (var t2 = -4; t2 <= 4; t2 += 2) r.rect(cx + t2, cy + 5, 1, 2, C(R.FLESH, 3));
        } else if (mode === 'ouch') {
            r.ellipse(cx, cy + 7, 4, 3.5, C(R.BLOOD, 1));
            r.ellipse(cx, cy + 7, 2.4, 2, C(R.BLOOD, 4));
        } else {
            r.rect(cx - 4, cy + 6, 8, 2, C(R.BLOOD, 2));
        }

        // Injuries accumulate as health drops: cuts, then a bloodied face.
        if (pain >= 1) {
            r.line(cx + 2, cy - 6, cx + 7, cy - 2, C(R.BLOOD, 6));
            r.set(cx + 5, cy - 4, C(R.BLOOD, 9));
        }
        if (pain >= 2) {
            r.line(cx - 7, cy - 3, cx - 3, cy + 2, C(R.BLOOD, 6));
            r.rect(cx - 8, cy + 3, 3, 2, C(R.BLOOD, 5));
        }
        if (pain >= 3) {
            r.ellipse(cx - 5, cy + 4, 3, 2, C(R.BLOOD, 5));
            r.ellipse(cx + 6, cy + 2, 2.5, 3, C(R.BLOOD, 5));
            r.rect(cx - 2, cy - 9, 5, 3, C(R.BLOOD, 7));
        }
        if (pain >= 4) {
            for (var i = 0; i < 16; i++) {
                var a = (i / 16) * U.PI2;
                r.circle(cx + Math.cos(a) * 8, cy + 2 + Math.sin(a) * 9, 1.6, C(R.BLOOD, 4 + (i % 4)));
            }
            r.ellipse(cx, cy + 10, 6, 2, C(R.BLOOD, 3));
        }
        return r;
    }

    // Faces are rebuilt only when the state changes, keyed by their signature.
    var faceCache = {};
    function faceFor(pain, dir, mode) {
        var key = pain + '|' + dir + '|' + mode;
        if (!faceCache[key]) faceCache[key] = drawFace({ pain: pain, dir: dir, mode: mode });
        return faceCache[key];
    }

    // ------------------------------------------------------------- status bar

    var BAR_W = 400, BAR_H = 48;   // drawn at this size, scaled x2 on output

    var barBg = null;

    function buildBarBackground() {
        if (barBg) return barBg;
        var r = new Raster(BAR_W, BAR_H, C(R.STEEL, 3));
        var rng = U.makeRng(0xBADF00D);
        r.vgrad(0, 0, BAR_W, BAR_H, R.STEEL, 5, 2);
        // brushed-metal striping and a bevelled top edge
        for (var y = 0; y < BAR_H; y += 4) r.rect(0, y, BAR_W, 1, C(R.STEEL, 2));
        r.rect(0, 0, BAR_W, 2, C(R.STEEL, 9));
        r.rect(0, 2, BAR_W, 1, C(R.STEEL, 6));
        r.rect(0, BAR_H - 2, BAR_W, 2, C(R.STEEL, 1));
        r.speckle(0, 3, BAR_W, BAR_H - 5, rng, 1);

        // recessed panels behind each readout
        var panels = [[3, 4, 66, 40], [73, 4, 74, 40], [151, 4, 78, 40],
                      [233, 4, 34, 40], [271, 4, 74, 40], [349, 4, 48, 40]];
        for (var i = 0; i < panels.length; i++) {
            var p = panels[i];
            r.rect(p[0], p[1], p[2], p[3], C(R.STEEL, 1));
            r.frame(p[0], p[1], p[2], p[3], C(R.STEEL, 6), 1);
            r.rect(p[0] + 1, p[1] + 1, p[2] - 2, 1, C(R.STEEL, 0));
        }
        barBg = r;
        return r;
    }

    var BIG = 3, SMALL = 1;

    /**
     * Render the whole status bar into a fresh raster.
     * @param {object} s {health, armor, ammo, ammoType, weapons, keys, face}
     */
    function drawStatusBar(s) {
        var bg = buildBarBackground();
        var r = new Raster(BAR_W, BAR_H, 0);
        r.px.set(bg.px);

        var red = C(R.RED, 13);
        var dim = C(R.STEEL, 6);
        var shadow = C(R.BLOOD, 0);

        // ---- ammo for the weapon in hand
        var ammoText = s.ammoType ? String(s.ammo) : '';
        if (ammoText) {
            drawText(r, ammoText, 66 - textWidth(ammoText, BIG), 10, BIG, red, shadow);
        }
        drawText(r, 'AMMO', 22, 34, SMALL, dim);

        // ---- health
        var hp = String(Math.max(0, Math.round(s.health))) + '%';
        drawText(r, hp, 143 - textWidth(hp, BIG), 10, BIG, red, shadow);
        drawText(r, 'HEALTH', 92, 34, SMALL, dim);

        // ---- ARMS: which weapons are in the pack
        drawText(r, 'ARMS', 180, 34, SMALL, dim);
        for (var i = 1; i < W.WEAPONS.length; i++) {
            var col = 155 + ((i - 1) % 4) * 19;
            var row = 8 + (((i - 1) / 4) | 0) * 13;
            drawText(r, String(i + 1), col, row, 2, s.weapons[i] ? C(R.YELLOW, 14) : C(R.STEEL, 4));
        }

        // ---- face
        var face = faceFor(s.face.pain, s.face.dir, s.face.mode);
        r.blit(face, 234, 8);

        // ---- armor
        var ar = String(Math.max(0, Math.round(s.armor))) + '%';
        drawText(r, ar, 341 - textWidth(ar, BIG), 10, BIG, red, shadow);
        drawText(r, 'ARMOR', 290, 34, SMALL, dim);

        // ---- keycards
        var keyRamps = [R.BLUE, R.YELLOW, R.RED];
        var keyNames = ['blue', 'yellow', 'red'];
        for (var k = 0; k < 3; k++) {
            var kx = 351, ky = 6 + k * 8;
            if (s.keys[keyNames[k]]) {
                r.rect(kx, ky, 8, 6, C(keyRamps[k], 12));
                r.frame(kx, ky, 8, 6, C(keyRamps[k], 5), 1);
                r.rect(kx + 2, ky + 1, 4, 2, C(keyRamps[k], 15));
            } else {
                r.frame(kx, ky, 8, 6, C(R.STEEL, 3), 1);
            }
        }

        // ---- ammo reserves, small, on the right
        var types = [['BULL', 'bullets'], ['SHEL', 'shells'], ['CELL', 'cells']];
        for (var t = 0; t < types.length; t++) {
            var y = 6 + t * 10;
            drawText(r, types[t][0], 362, y, SMALL, dim);
            var val = String(s.ammoAll[types[t][1]] || 0);
            drawText(r, val, 396 - textWidth(val, SMALL), y,
                SMALL, s.ammoType === types[t][1] ? C(R.YELLOW, 14) : C(R.STEEL, 8));
        }
        return r;
    }

    /** Face state machine: what expression the player is wearing right now. */
    function faceState(p, tick) {
        if (p.dead) return { pain: 4, dir: 0, mode: 'dead' };
        var pain = U.clamp(Math.floor((100 - p.health) * 5 / 101), 0, 4);
        if (p.godMode) return { pain: pain, dir: 0, mode: 'god' };
        if (p.grinTime > 0) return { pain: pain, dir: 0, mode: 'evil' };
        if (p.ouchTime > 0) {
            // turn towards whatever just hurt him
            var d = p.painDir === undefined ? 0 : p.painDir;
            return { pain: pain, dir: d, mode: 'ouch' };
        }
        // idle glance, changing about once a second
        var look = [0, -1, 0, 1][(tick / 35 | 0) & 3];
        return { pain: pain, dir: look, mode: 'normal' };
    }

    DOOM.HUD = {
        GLYPHS: GLYPHS,
        GLYPH_W: GLYPH_W,
        GLYPH_H: GLYPH_H,
        BAR_W: BAR_W,
        BAR_H: BAR_H,
        FACE_W: FACE_W,
        FACE_H: FACE_H,
        textWidth: textWidth,
        drawText: drawText,
        drawTextCanvas: drawTextCanvas,
        drawTextCentered: drawTextCentered,
        drawFace: drawFace,
        faceFor: faceFor,
        faceState: faceState,
        drawStatusBar: drawStatusBar
    };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 11_game.js --- */

/* ==========================================================================
 * DOOM :: game.js -- player, world simulation, level flow and the main loop
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;
    var M = DOOM.Maps;
    var W = DOOM.Weapons;
    var Mon = DOOM.Monsters;
    var HUD = DOOM.HUD;
    var Sound = DOOM.Sound;
    var Music = DOOM.Music;

    var RENDER_W = 640, RENDER_H = 400;
    var VIEW_W = 800, VIEW_H = 504;
    var CANVAS_W = 800, CANVAS_H = 600;
    var BAR_H_OUT = CANVAS_H - VIEW_H;

    var PLAYER_RADIUS = 0.26;
    var USE_RANGE = 1.35;
    var FOV = 66 * Math.PI / 180;

    // Pickup table: what each item gives and what it says when you take it.
    var PICKUPS = {
        medikit:    { give: 'health', amount: 25, max: 100, msg: 'PICKED UP A MEDIKIT.', sound: 'pickup' },
        stimpack:   { give: 'health', amount: 10, max: 100, msg: 'PICKED UP A STIMPACK.', sound: 'pickup' },
        soulsphere: { give: 'health', amount: 100, max: 200, msg: 'SUPERCHARGE!', sound: 'keyPickup' },
        armor:      { give: 'armor', amount: 100, absorb: 1 / 3, msg: 'PICKED UP THE ARMOR.', sound: 'pickup' },
        megaarmor:  { give: 'armor', amount: 200, absorb: 1 / 2, msg: 'PICKED UP THE MEGAARMOR!', sound: 'keyPickup' },
        clip:       { give: 'ammo', ammo: 'bullets', amount: 10, msg: 'PICKED UP A CLIP.', sound: 'pickup' },
        ammobox:    { give: 'ammo', ammo: 'bullets', amount: 50, msg: 'PICKED UP A BOX OF BULLETS.', sound: 'pickup' },
        shells:     { give: 'ammo', ammo: 'shells', amount: 4, msg: 'PICKED UP 4 SHOTGUN SHELLS.', sound: 'pickup' },
        shellbox:   { give: 'ammo', ammo: 'shells', amount: 20, msg: 'PICKED UP A BOX OF SHELLS.', sound: 'pickup' },
        cell:       { give: 'ammo', ammo: 'cells', amount: 20, msg: 'PICKED UP AN ENERGY CELL.', sound: 'pickup' },
        cellpack:   { give: 'ammo', ammo: 'cells', amount: 100, msg: 'PICKED UP AN ENERGY CELL PACK.', sound: 'pickup' },
        shotgun:    { give: 'weapon', weapon: 'shotgun', ammo: 'shells', amount: 8, msg: 'YOU GOT THE SHOTGUN!', sound: 'weaponPickup' },
        chaingun:   { give: 'weapon', weapon: 'chaingun', ammo: 'bullets', amount: 20, msg: 'YOU GOT THE CHAINGUN!', sound: 'weaponPickup' },
        plasma:     { give: 'weapon', weapon: 'plasma', ammo: 'cells', amount: 40, msg: 'YOU GOT THE PLASMA GUN!', sound: 'weaponPickup' },
        redkey:     { give: 'key', key: 'red', msg: 'PICKED UP A RED KEYCARD.', sound: 'keyPickup' },
        bluekey:    { give: 'key', key: 'blue', msg: 'PICKED UP A BLUE KEYCARD.', sound: 'keyPickup' },
        yellowkey:  { give: 'key', key: 'yellow', msg: 'PICKED UP A YELLOW KEYCARD.', sound: 'keyPickup' }
    };

    // Non-collectable scenery. Barrels are the only ones you can shoot.
    var DECOR = {
        barrel: { worldH: 0.55, radius: 0.32, hp: 20, explodes: true },
        lamp:   { worldH: 0.7, radius: 0.25, light: 8, glow: true },
        gore:   { worldH: 0.8, radius: 0.25 }
    };

    var PROJECTILES = {
        fireball:  { speed: 9, damage: [3, 24], radius: 0.22, sprite: 'fireball', worldH: 0.3, light: 12, blast: 0 },
        baronball: { speed: 10, damage: [8, 64], radius: 0.24, sprite: 'baronball', worldH: 0.36, light: 12, blast: 0 },
        rocket:    { speed: 11, damage: [20, 128], radius: 0.25, sprite: 'rocket', worldH: 0.32, light: 10, blast: 2.6, blastDamage: 90 },
        plasma:    { speed: 22, damage: [5, 40], radius: 0.18, sprite: 'plasma', worldH: 0.26, light: 14, blast: 0, friendly: true }
    };

    function rnd(range) { return range[0] + Math.random() * (range[1] - range[0]); }

    // ====================================================================== Game

    function Game(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.ctx.imageSmoothingEnabled = false;

        this.renderer = new DOOM.Renderer(RENDER_W, RENDER_H);
        this.textures = DOOM.Textures.build();
        this.sprites = DOOM.Sprites.build();
        this.renderer.setAssets(this.textures, this.sprites);
        W.buildViews();

        // offscreen surfaces: the 3D view and the status bar, both scaled up
        this.viewCanvas = makeCanvas(RENDER_W, RENDER_H);
        this.viewCtx = this.viewCanvas.getContext('2d');
        this.viewImage = this.viewCtx.createImageData(RENDER_W, RENDER_H);
        this.viewPixels = new Uint32Array(this.viewImage.data.buffer);

        this.barCanvas = makeCanvas(HUD.BAR_W, HUD.BAR_H);
        this.barCtx = this.barCanvas.getContext('2d');
        this.barImage = this.barCtx.createImageData(HUD.BAR_W, HUD.BAR_H);
        this.barPixels = new Uint32Array(this.barImage.data.buffer);

        this.state = 'title';
        this.skill = 1;
        this.menuIndex = 0;
        this.tick = 0;
        this.time = 0;
        this.levelIndex = 0;
        this.messages = [];
        this.keysDown = {};
        this.mouseFire = false;
        this.musicMuted = false;
        this.soundMuted = false;
        this.titleT = 0;
        this.totals = { kills: 0, items: 0, secrets: 0 };
        this.player = null;
    }

    function makeCanvas(w, h) {
        var c = document.createElement('canvas');
        c.width = w;
        c.height = h;
        return c;
    }

    // ------------------------------------------------------------------ levels

    Game.prototype.startGame = function (skill) {
        this.skill = skill;
        this.levelIndex = 0;
        this.player = this.makePlayer();
        this.loadLevel(0);
        this.state = 'play';
    };

    Game.prototype.makePlayer = function () {
        var owned = [true, true, false, false, false];
        return {
            x: 0, y: 0, ang: 0,
            vx: 0, vy: 0,
            health: 100, armor: 0, armorAbsorb: 0,
            ammo: { bullets: 50, shells: 0, cells: 0 },
            weapons: owned,
            keys: { red: false, blue: false, yellow: false },
            dead: false,
            godMode: false,
            bob: 0, bobPhase: 0,
            weaponState: W.makeState(1),
            damageFlash: 0, pickupFlash: 0, radFlash: 0,
            grinTime: 0, ouchTime: 0, painDir: 0,
            deathTilt: 0,
            lastDamage: 0
        };
    };

    Game.prototype.loadLevel = function (index) {
        var self = this;
        this.levelIndex = index;
        this.map = M.load(index);
        this.doors = {};
        this.monsters = [];
        this.items = [];
        this.decor = [];
        this.projectiles = [];
        this.effects = [];
        this.secretsFound = {};
        this.stats = { kills: 0, killsTotal: 0, items: 0, itemsTotal: 0, secrets: 0, secretsTotal: this.map.secretCount };
        this.levelTime = 0;
        this.exiting = 0;
        this.bossAlive = false;
        this.messages = [];

        var p = this.player;
        p.x = this.map.start.x;
        p.y = this.map.start.y;
        p.ang = this.map.start.a;
        p.dead = false;
        p.deathTilt = 0;
        p.weaponState = W.makeState(W.bestAvailable(p.weapons, p.ammo));
        if (p.health <= 0) p.health = 100;

        var minSkill = Mon.SKILLS[this.skill].minThing;
        this.map.things.forEach(function (t) {
            if (t.skill && t.skill > minSkill) return;
            if (Mon.MONSTERS[t.t]) {
                var m = Mon.spawn(t.t, t.x, t.y);
                m.ang = Math.random() * U.PI2;
                self.monsters.push(m);
                self.stats.killsTotal++;
                if (t.boss) { m.isBoss = true; self.bossAlive = true; }
            } else if (PICKUPS[t.t]) {
                self.items.push({ type: t.t, x: t.x, y: t.y, taken: false, def: PICKUPS[t.t] });
                self.stats.itemsTotal++;
            } else if (DECOR[t.t]) {
                self.decor.push({
                    type: t.t, x: t.x, y: t.y, def: DECOR[t.t],
                    hp: DECOR[t.t].hp || 0, dead: false
                });
            }
        });

        this.pushMessage(this.map.name);
        if (!this.musicMuted) Music.start(this.map.music);
    };

    Game.prototype.pushMessage = function (text) {
        this.messages.push({ text: text, t: 0 });
        if (this.messages.length > 4) this.messages.shift();
    };

    // ------------------------------------------------------------------- doors

    Game.prototype.doorAt = function (x, y) {
        var key = x + ',' + y;
        var d = this.doors[key];
        if (!d) {
            var flags = M.tileFlags(this.map, x, y);
            if (!(flags & M.F_DOOR)) return null;
            d = this.doors[key] = {
                x: x, y: y, open: 0, state: 'closed', timer: 0,
                key: this.map.doorKey[y * this.map.w + x]
            };
        }
        return d;
    };

    Game.prototype.doorOpenness = function (x, y) {
        var d = this.doors[x + ',' + y];
        return d ? d.open : 0;
    };

    /** Try to open a door; refuses (and complains) without the right keycard. */
    Game.prototype.tryOpenDoor = function (d, byPlayer) {
        if (d.state === 'open' || d.state === 'opening') {
            d.timer = 4;
            return true;
        }
        if (d.key && d.key !== 'none') {
            if (!this.player.keys[d.key]) {
                if (byPlayer) {
                    Sound.play('locked', 0.8);
                    this.pushMessage('YOU NEED A ' + d.key.toUpperCase() + ' KEYCARD TO OPEN THIS DOOR.');
                }
                return false;
            }
        }
        d.state = 'opening';
        Sound.playAt('doorOpen', U.dist(this.player.x, this.player.y, d.x + 0.5, d.y + 0.5));
        return true;
    };

    Game.prototype.updateDoors = function (dt) {
        var p = this.player;
        for (var k in this.doors) {
            if (!Object.prototype.hasOwnProperty.call(this.doors, k)) continue;
            var d = this.doors[k];
            if (d.state === 'opening') {
                d.open = Math.min(1, d.open + dt * 1.1);
                if (d.open >= 1) { d.state = 'open'; d.timer = 4; }
            } else if (d.state === 'open') {
                d.timer -= dt;
                // never close on the player's head
                var blocked = Math.abs(p.x - (d.x + 0.5)) < 0.9 && Math.abs(p.y - (d.y + 0.5)) < 0.9;
                if (d.timer <= 0 && !blocked) {
                    d.state = 'closing';
                    Sound.playAt('doorClose', U.dist(p.x, p.y, d.x + 0.5, d.y + 0.5));
                }
            } else if (d.state === 'closing') {
                d.open = Math.max(0, d.open - dt * 0.9);
                if (d.open <= 0) d.state = 'closed';
            }
        }

        // Unkeyed doors slide open when you walk up to them.
        var pxi = p.x | 0, pyi = p.y | 0;
        for (var oy = -1; oy <= 1; oy++) {
            for (var ox = -1; ox <= 1; ox++) {
                var tx = pxi + ox, ty = pyi + oy;
                if (!(M.tileFlags(this.map, tx, ty) & M.F_DOOR)) continue;
                var dd = this.doorAt(tx, ty);
                if (!dd || (dd.key && dd.key !== 'none')) continue;
                if (U.dist(p.x, p.y, tx + 0.5, ty + 0.5) < 1.15) this.tryOpenDoor(dd, false);
            }
        }
    };

    // -------------------------------------------------------------- collision

    Game.prototype.blockedAt = function (x, y, radius) {
        var map = this.map;
        var minX = Math.floor(x - radius), maxX = Math.floor(x + radius);
        var minY = Math.floor(y - radius), maxY = Math.floor(y + radius);
        for (var ty = minY; ty <= maxY; ty++) {
            for (var tx = minX; tx <= maxX; tx++) {
                if (!M.inBounds(map, tx, ty)) return true;
                var f = map.flags[ty * map.w + tx];
                if (f & (M.F_SOLID | M.F_WINDOW)) return true;
                if (f & M.F_DOOR) {
                    var d = this.doorAt(tx, ty);
                    if (!d || d.open < 0.72) return true;
                }
            }
        }
        return false;
    };

    /** Move with wall sliding: try both axes, then each separately. */
    Game.prototype.moveEntity = function (e, dx, dy, radius) {
        var moved = false;
        if (!this.blockedAt(e.x + dx, e.y, radius)) { e.x += dx; moved = moved || dx !== 0; }
        if (!this.blockedAt(e.x, e.y + dy, radius)) { e.y += dy; moved = moved || dy !== 0; }
        return moved;
    };

    /** Monsters also collide with each other and with barrels. */
    Game.prototype.monsterBlocked = function (m, x, y) {
        if (this.blockedAt(x, y, m.radius)) return true;
        for (var i = 0; i < this.monsters.length; i++) {
            var o = this.monsters[i];
            if (o === m || o.state === Mon.S_DEATH || o.state === Mon.S_GONE) continue;
            var rr = m.radius + o.radius;
            if (U.dist2(x, y, o.x, o.y) < rr * rr) return true;
        }
        for (var k = 0; k < this.decor.length; k++) {
            var d = this.decor[k];
            if (d.dead || !d.def.radius) continue;
            var r2 = m.radius + d.def.radius;
            if (U.dist2(x, y, d.x, d.y) < r2 * r2) return true;
        }
        return false;
    };

    // -------------------------------------------------------------- hitscan

    /**
     * Trace a hitscan shot. Returns the entity hit (monster or barrel) and the
     * impact point, or the wall impact if nothing living is in the way.
     */
    Game.prototype.traceShot = function (ox, oy, ang, range, ignore) {
        var self = this;
        var wallDist = DOOM.rayTrace(this.map, ox, oy, ang, range, function (x, y) {
            return self.doorOpenness(x, y);
        });
        var dx = Math.cos(ang), dy = Math.sin(ang);
        var best = null, bestT = wallDist;

        function test(e, radius) {
            var ex = e.x - ox, ey = e.y - oy;
            var along = ex * dx + ey * dy;
            if (along <= 0.1 || along >= bestT) return;
            var perp = Math.abs(ex * dy - ey * dx);
            if (perp > radius) return;
            bestT = along;
            best = e;
        }

        for (var i = 0; i < this.monsters.length; i++) {
            var m = this.monsters[i];
            if (m === ignore || m.state === Mon.S_DEATH || m.state === Mon.S_GONE) continue;
            test(m, m.radius + 0.06);
        }
        for (var k = 0; k < this.decor.length; k++) {
            var d = this.decor[k];
            if (d.dead || !d.def.explodes) continue;
            test(d, d.def.radius);
        }
        return { entity: best, dist: bestT, x: ox + dx * bestT, y: oy + dy * bestT, wall: best === null };
    };

    Game.prototype.spawnEffect = function (kind, x, y, zOff, scale) {
        this.effects.push({
            kind: kind, x: x, y: y, zOff: zOff === undefined ? 0.35 : zOff,
            t: 0, scale: scale || 1
        });
    };

    // -------------------------------------------------------------- explosions

    Game.prototype.explode = function (x, y, damage, radius, source) {
        var self = this;
        this.spawnEffect('explosion', x, y, 0.3, radius / 2.2);
        Sound.playAt('explosion', U.dist(this.player.x, this.player.y, x, y));

        function falloff(ex, ey) {
            var d = U.dist(x, y, ex, ey);
            if (d > radius) return 0;
            return damage * (1 - d / radius);
        }

        for (var i = 0; i < this.monsters.length; i++) {
            var m = this.monsters[i];
            if (m.state === Mon.S_DEATH || m.state === Mon.S_GONE) continue;
            var dmg = falloff(m.x, m.y);
            if (dmg > 0) this.hurtMonster(m, dmg);
        }
        var pd = falloff(this.player.x, this.player.y);
        if (pd > 0) this.hurtPlayer(pd, x, y);

        // chain reaction, one frame later so the blasts stagger audibly
        for (var k = 0; k < this.decor.length; k++) {
            var d2 = this.decor[k];
            if (d2.dead || !d2.def.explodes || d2 === source) continue;
            if (falloff(d2.x, d2.y) > 0 && !d2.fuse) d2.fuse = 0.12 + Math.random() * 0.12;
        }
    };

    Game.prototype.hurtDecor = function (d, damage) {
        if (d.dead || !d.def.explodes) return;
        d.hp -= damage;
        if (d.hp <= 0 && !d.fuse) d.fuse = 0.01;
    };

    Game.prototype.hurtMonster = function (m, damage) {
        var self = this;
        var wasAlive = m.state !== Mon.S_DEATH;
        var res = Mon.damage(m, damage, {
            sound: function (name, ent, pitch) {
                Sound.playAt(name, U.dist(self.player.x, self.player.y, ent.x, ent.y), pitch);
            },
            onDeath: function () {
                self.stats.kills++;
                if (m.isBoss) {
                    self.bossAlive = false;
                    self.pushMessage('THE ANOMALY IS SILENT. THE TELEPORTER IS ACTIVE.');
                }
            }
        });
        if (wasAlive) this.spawnEffect('blood', m.x, m.y, m.def.worldH * 0.55);
        return res;
    };

    // ---------------------------------------------------------------- player

    Game.prototype.hurtPlayer = function (damage, srcX, srcY) {
        var p = this.player;
        if (p.dead || p.godMode) return;
        damage *= Mon.SKILLS[this.skill].damageTaken;

        // Armour soaks a fraction and is consumed doing so.
        if (p.armor > 0 && p.armorAbsorb > 0) {
            var soak = Math.min(p.armor, damage * p.armorAbsorb);
            p.armor -= soak;
            damage -= soak;
            if (p.armor <= 0) p.armorAbsorb = 0;
        }
        p.health -= damage;
        p.damageFlash = Math.min(0.75, p.damageFlash + damage / 60);
        p.ouchTime = 0.7;
        if (srcX !== undefined) {
            var rel = U.angleDiff(Math.atan2(srcY - p.y, srcX - p.x), p.ang);
            p.painDir = rel > 0.3 ? 1 : (rel < -0.3 ? -1 : 0);
        }
        if (p.health <= 0) {
            p.health = 0;
            p.dead = true;
            Sound.play('playerDeath', 1);
            Music.stop();
            this.pushMessage('YOU DIED');
        } else {
            Sound.play(damage > 18 ? 'playerPain' : 'oof', 0.9);
        }
    };

    Game.prototype.givePickup = function (item) {
        var p = this.player, d = item.def, took = false;
        switch (d.give) {
            case 'health':
                if (p.health < d.max) { p.health = Math.min(d.max, p.health + d.amount); took = true; }
                break;
            case 'armor':
                if (p.armor < d.amount) { p.armor = d.amount; p.armorAbsorb = d.absorb; took = true; }
                break;
            case 'ammo':
                var cap = W.AMMO_MAX[d.ammo];
                if (p.ammo[d.ammo] < cap) {
                    p.ammo[d.ammo] = Math.min(cap, p.ammo[d.ammo] + d.amount * Mon.SKILLS[this.skill].ammoBonus);
                    took = true;
                }
                break;
            case 'weapon':
                var wi = W.byId(d.weapon);
                var isNew = !p.weapons[wi];
                p.weapons[wi] = true;
                p.ammo[d.ammo] = Math.min(W.AMMO_MAX[d.ammo], p.ammo[d.ammo] + d.amount);
                if (isNew) { p.grinTime = 1.1; p.weaponState.wantSwitch = wi; }
                took = true;
                break;
            case 'key':
                p.keys[d.key] = true;
                took = true;
                break;
        }
        if (!took) return false;
        item.taken = true;
        this.stats.items++;
        p.pickupFlash = 0.32;
        Sound.play(d.sound, 0.8);
        this.pushMessage(d.msg);
        return true;
    };

    Game.prototype.fireWeapon = function (index) {
        var p = this.player, wp = W.WEAPONS[index];
        var self = this;
        if (wp.ammo) p.ammo[wp.ammo] = Math.max(0, p.ammo[wp.ammo] - wp.use);

        if (wp.melee) {
            Sound.play('swish', 0.7);
            var hit = this.traceShot(p.x, p.y, p.ang, wp.range);
            if (hit.entity && hit.dist <= wp.range) {
                Sound.play('punch', 0.9);
                if (hit.entity.def && hit.entity.def.hp !== undefined && hit.entity.type in DECOR) {
                    this.hurtDecor(hit.entity, rnd(wp.damage));
                } else {
                    this.hurtMonster(hit.entity, rnd(wp.damage));
                }
            }
            return;
        }

        Sound.play(wp.sound, 1);
        if (wp.projectile) {
            this.launchProjectile(p.x, p.y, p.ang, wp.projectile, null);
            return;
        }

        for (var i = 0; i < wp.pellets; i++) {
            var ang = p.ang + (Math.random() - 0.5) * 2 * wp.spread;
            var res = this.traceShot(p.x, p.y, ang, wp.range);
            var dmg = rnd(wp.damage);
            if (res.entity) {
                if (DECOR[res.entity.type]) {
                    this.hurtDecor(res.entity, dmg);
                    this.spawnEffect('puff', res.x, res.y, 0.4);
                } else {
                    this.hurtMonster(res.entity, dmg);
                }
            } else {
                this.spawnEffect('puff', res.x - Math.cos(ang) * 0.1, res.y - Math.sin(ang) * 0.1, 0.45);
                if (i === 0) Sound.playAt('wallhit', res.dist);
            }
        }
    };

    Game.prototype.launchProjectile = function (x, y, ang, typeId, owner) {
        var def = PROJECTILES[typeId];
        var sx = x + Math.cos(ang) * 0.45, sy = y + Math.sin(ang) * 0.45;
        this.projectiles.push({
            type: typeId, def: def, owner: owner,
            x: sx, y: sy,
            vx: Math.cos(ang) * def.speed,
            vy: Math.sin(ang) * def.speed,
            t: 0, life: 6
        });
    };

    // ----------------------------------------------------------------- update

    Game.prototype.update = function (dt) {
        this.tick++;
        this.time += dt;

        if (this.state === 'title' || this.state === 'skill' || this.state === 'help') {
            this.titleT += dt;
            return;
        }
        if (this.state === 'intermission') { this.interT += dt; return; }
        if (this.state === 'victory') { this.titleT += dt; return; }
        if (this.state !== 'play' && this.state !== 'dead') return;

        this.levelTime += dt;
        var p = this.player;

        if (p.dead) {
            p.deathTilt = Math.min(1, p.deathTilt + dt * 1.4);
            this.updateEffects(dt);
            this.updateProjectiles(dt);
            if (p.deathTilt >= 1 && this.state !== 'dead') this.state = 'dead';
            return;
        }

        this.updatePlayer(dt);
        this.updateDoors(dt);
        this.updateMonsters(dt);
        this.updateProjectiles(dt);
        this.updateItems(dt);
        this.updateDecor(dt);
        this.updateEffects(dt);

        for (var i = 0; i < this.messages.length; i++) this.messages[i].t += dt;

        if (this.exiting > 0) {
            this.exiting -= dt;
            if (this.exiting <= 0) this.finishLevel();
        }
    };

    Game.prototype.updatePlayer = function (dt) {
        var p = this.player, k = this.keysDown;
        var run = k['ShiftLeft'] || k['ShiftRight'];
        var speed = (run ? 5.6 : 3.2);
        var turn = (run ? 3.4 : 2.4);

        var fwd = 0, side = 0;
        if (k['KeyW'] || k['ArrowUp']) fwd += 1;
        if (k['KeyS'] || k['ArrowDown']) fwd -= 1;
        if (k['KeyA']) side -= 1;
        if (k['KeyD']) side += 1;
        if (k['ArrowLeft']) p.ang -= turn * dt;
        if (k['ArrowRight']) p.ang += turn * dt;
        if (k['KeyQ']) side -= 1;
        if (k['KeyE']) side += 1;
        p.ang = U.normAngle(p.ang);

        var len = Math.sqrt(fwd * fwd + side * side);
        if (len > 0) {
            fwd /= len; side /= len;
            var dx = (Math.cos(p.ang) * fwd - Math.sin(p.ang) * side) * speed * dt;
            var dy = (Math.sin(p.ang) * fwd + Math.cos(p.ang) * side) * speed * dt;
            this.moveEntity(p, dx, dy, PLAYER_RADIUS);
            p.bobPhase += dt * (run ? 13 : 9);
        } else {
            p.bobPhase += dt * 2.5;
        }
        p.bob = Math.sin(p.bobPhase) * (len > 0 ? (run ? 5.5 : 3.5) : 0.7);

        // floor effects: radiation burns, secrets found
        var tx = p.x | 0, ty = p.y | 0;
        if (M.inBounds(this.map, tx, ty)) {
            var cell = ty * this.map.w + tx;
            var f = this.map.flags[cell];
            if (f & M.F_DAMAGE) {
                p.radAccum = (p.radAccum || 0) + dt;
                if (p.radAccum >= 0.55) {
                    p.radAccum = 0;
                    this.hurtPlayer(this.map.damage[cell]);
                    p.radFlash = 0.3;
                    Sound.play('nukageBurn', 0.5);
                }
            }
            if (f & M.F_SECRET) {
                var sid = this.map.secretId[cell];
                if (sid >= 0 && !this.secretsFound[sid]) {
                    this.secretsFound[sid] = true;
                    this.stats.secrets++;
                    Sound.play('secret', 0.9);
                    this.pushMessage('A SECRET IS REVEALED!');
                }
            }
            if ((f & M.F_TELEPORT) && this.canExit()) this.requestExit(true);
        }

        // weapon
        var self = this;
        var want = -1;
        for (var wi = 0; wi < W.WEAPONS.length; wi++) {
            if (k['Digit' + (wi + 1)]) want = wi;
        }
        if (p.weaponState.wantSwitch !== undefined && p.weaponState.wantSwitch >= 0) {
            want = p.weaponState.wantSwitch;
            p.weaponState.wantSwitch = -1;
        }
        var firing = !!(this.mouseFire || k['ControlLeft'] || k['ControlRight']);
        W.update(p.weaponState, dt, {
            firing: firing,
            want: want,
            owned: p.weapons,
            ammo: p.ammo,
            onFire: function (idx) { self.fireWeapon(idx); },
            onEmpty: function () {
                if (!p.emptyCooldown || p.emptyCooldown <= 0) {
                    p.emptyCooldown = 0.5;
                    var best = W.bestAvailable(p.weapons, p.ammo);
                    if (best !== p.weaponState.index) p.weaponState.wantSwitch = best;
                }
            }
        });
        p.weaponState.spinFrame = ((p.weaponState.spin * this.tick * 0.9) | 0) & 3;
        if (p.emptyCooldown > 0) p.emptyCooldown -= dt;

        p.damageFlash = Math.max(0, p.damageFlash - dt * 1.6);
        p.pickupFlash = Math.max(0, p.pickupFlash - dt * 1.6);
        p.radFlash = Math.max(0, p.radFlash - dt * 1.6);
        p.grinTime = Math.max(0, p.grinTime - dt);
        p.ouchTime = Math.max(0, p.ouchTime - dt);
    };

    Game.prototype.canExit = function () {
        return !this.map.bossLevel || !this.bossAlive;
    };

    Game.prototype.requestExit = function (teleport) {
        if (this.exiting > 0) return;
        if (!this.canExit()) {
            this.pushMessage('THE TELEPORTER IS DEAD. SOMETHING HERE STILL BREATHES.');
            Sound.play('locked', 0.8);
            return;
        }
        this.exiting = teleport ? 1.1 : 0.7;
        Sound.play(teleport ? 'teleport' : 'switchFlip', 1);
        Sound.play('levelDone', 0.8);
    };

    /** The "use" key: open a door or throw a switch in front of the player. */
    Game.prototype.useAction = function () {
        var p = this.player;
        if (p.dead) return;
        var dx = Math.cos(p.ang), dy = Math.sin(p.ang);
        for (var s = 0.5; s <= USE_RANGE; s += 0.25) {
            var tx = (p.x + dx * s) | 0, ty = (p.y + dy * s) | 0;
            if (!M.inBounds(this.map, tx, ty)) return;
            var f = this.map.flags[ty * this.map.w + tx];
            if (f & M.F_DOOR) {
                var d = this.doorAt(tx, ty);
                if (d) this.tryOpenDoor(d, true);
                return;
            }
            if (f & M.F_SWITCH) {
                if (this.map.action[ty * this.map.w + tx] === 'exit') this.requestExit(false);
                return;
            }
            if (f & (M.F_SOLID | M.F_WINDOW)) return;
        }
    };

    Game.prototype.finishLevel = function () {
        Music.stop();
        this.interT = 0;
        this.interShown = 0;
        this.state = 'intermission';
    };

    Game.prototype.nextLevel = function () {
        if (this.levelIndex + 1 >= M.count) {
            this.state = 'victory';
            this.titleT = 0;
            Music.start('victory');
            return;
        }
        this.loadLevel(this.levelIndex + 1);
        this.state = 'play';
    };

    // ---------------------------------------------------------------- entities

    Game.prototype.updateMonsters = function (dt) {
        var self = this, p = this.player;
        var world = {
            player: p,
            dt: dt,
            skill: Mon.SKILLS[this.skill],
            canSee: function (m) {
                return DOOM.lineOfSight(self.map, m.x, m.y, p.x, p.y, function (x, y) {
                    return self.doorOpenness(x, y);
                });
            },
            tryMove: function (m, dx, dy) {
                var moved = false;
                if (!self.monsterBlocked(m, m.x + dx, m.y)) { m.x += dx; moved = true; }
                if (!self.monsterBlocked(m, m.x, m.y + dy)) { m.y += dy; moved = true; }
                return moved;
            },
            hitscan: function (m, damage, spread, pellets) {
                for (var i = 0; i < pellets; i++) {
                    var ang = Math.atan2(p.y - m.y, p.x - m.x) + (Math.random() - 0.5) * 2 * spread;
                    var res = self.traceShot(m.x, m.y, ang, m.def.attackRange, m);
                    if (res.entity === p || (!res.entity && self.playerOnRay(m, ang, res.dist))) {
                        self.hurtPlayer(rnd(damage), m.x, m.y);
                    } else if (res.entity && DECOR[res.entity.type]) {
                        self.hurtDecor(res.entity, rnd(damage));
                    } else if (res.entity) {
                        // monsters wound each other -- infighting, as intended
                        self.hurtMonster(res.entity, rnd(damage));
                    } else {
                        self.spawnEffect('puff', res.x, res.y, 0.45);
                    }
                }
            },
            launch: function (m, projId) {
                var ang = Math.atan2(p.y - m.y, p.x - m.x);
                self.launchProjectile(m.x, m.y, ang, projId, m);
            },
            meleeHit: function (m, damage) {
                if (U.dist(m.x, m.y, p.x, p.y) <= m.def.meleeRange + 0.4) {
                    self.hurtPlayer(rnd(damage), m.x, m.y);
                }
            },
            sound: function (name, ent, pitch) {
                Sound.playAt(name, U.dist(p.x, p.y, ent.x, ent.y), pitch);
            }
        };

        for (var i = 0; i < this.monsters.length; i++) {
            Mon.update(this.monsters[i], world);
        }
    };

    /** Did a monster's shot line pass close enough to the player to count? */
    Game.prototype.playerOnRay = function (m, ang, dist) {
        var p = this.player;
        var dx = Math.cos(ang), dy = Math.sin(ang);
        var ex = p.x - m.x, ey = p.y - m.y;
        var along = ex * dx + ey * dy;
        if (along <= 0 || along > dist + 0.05) return false;
        return Math.abs(ex * dy - ey * dx) <= PLAYER_RADIUS + 0.08;
    };

    Game.prototype.updateProjectiles = function (dt) {
        var p = this.player;
        for (var i = this.projectiles.length - 1; i >= 0; i--) {
            var pr = this.projectiles[i];
            pr.t += dt;
            var steps = Math.max(1, Math.ceil(pr.def.speed * dt / 0.2));
            var sdt = dt / steps;
            var boom = false, hitEnt = null;

            for (var s = 0; s < steps && !boom; s++) {
                pr.x += pr.vx * sdt;
                pr.y += pr.vy * sdt;
                if (this.blockedAt(pr.x, pr.y, pr.def.radius * 0.5)) { boom = true; break; }

                if (pr.owner) {
                    if (U.dist2(pr.x, pr.y, p.x, p.y) < Math.pow(PLAYER_RADIUS + pr.def.radius, 2)) {
                        hitEnt = p; boom = true; break;
                    }
                } else {
                    for (var k = 0; k < this.monsters.length; k++) {
                        var m = this.monsters[k];
                        if (m.state === Mon.S_DEATH || m.state === Mon.S_GONE) continue;
                        if (U.dist2(pr.x, pr.y, m.x, m.y) < Math.pow(m.radius + pr.def.radius, 2)) {
                            hitEnt = m; boom = true; break;
                        }
                    }
                    for (var b = 0; b < this.decor.length && !boom; b++) {
                        var d = this.decor[b];
                        if (d.dead || !d.def.explodes) continue;
                        if (U.dist2(pr.x, pr.y, d.x, d.y) < Math.pow(d.def.radius + pr.def.radius, 2)) {
                            hitEnt = d; boom = true; break;
                        }
                    }
                }
            }

            if (pr.t > pr.life) boom = true;

            if (boom) {
                this.projectiles.splice(i, 1);
                if (pr.def.blast > 0) {
                    this.explode(pr.x, pr.y, pr.def.blastDamage, pr.def.blast);
                } else {
                    this.spawnEffect(pr.def.friendly ? 'sparks' : 'explosion', pr.x, pr.y, 0.35,
                        pr.def.friendly ? 0.5 : 0.55);
                    Sound.playAt(pr.def.friendly ? 'plasmahit' : 'explosion',
                        U.dist(p.x, p.y, pr.x, pr.y));
                }
                if (hitEnt === p) {
                    this.hurtPlayer(rnd(pr.def.damage), pr.x, pr.y);
                } else if (hitEnt && DECOR[hitEnt.type]) {
                    this.hurtDecor(hitEnt, rnd(pr.def.damage));
                } else if (hitEnt) {
                    this.hurtMonster(hitEnt, rnd(pr.def.damage));
                }
            }
        }
    };

    Game.prototype.updateItems = function (dt) {
        var p = this.player;
        for (var i = 0; i < this.items.length; i++) {
            var it = this.items[i];
            if (it.taken) continue;
            if (U.dist2(p.x, p.y, it.x, it.y) < 0.36) this.givePickup(it);
        }
    };

    Game.prototype.updateDecor = function (dt) {
        for (var i = 0; i < this.decor.length; i++) {
            var d = this.decor[i];
            if (d.dead) continue;
            if (d.fuse !== undefined && d.fuse > 0) {
                d.fuse -= dt;
                if (d.fuse <= 0) {
                    d.dead = true;
                    this.explode(d.x, d.y, 128, 2.4, d);
                }
            }
        }
    };

    Game.prototype.updateEffects = function (dt) {
        for (var i = this.effects.length - 1; i >= 0; i--) {
            var e = this.effects[i];
            e.t += dt;
            var frames = this.sprites.fx[e.kind].length;
            if (e.t > frames * 0.055) this.effects.splice(i, 1);
        }
    };

    DOOM.Game = Game;
    DOOM.GameConst = {
        RENDER_W: RENDER_W, RENDER_H: RENDER_H,
        VIEW_W: VIEW_W, VIEW_H: VIEW_H,
        CANVAS_W: CANVAS_W, CANVAS_H: CANVAS_H,
        BAR_H_OUT: BAR_H_OUT,
        PLAYER_RADIUS: PLAYER_RADIUS,
        FOV: FOV,
        PICKUPS: PICKUPS,
        DECOR: DECOR,
        PROJECTILES: PROJECTILES
    };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 12_render.js --- */

/* ==========================================================================
 * DOOM :: render.js -- frame composition, HUD blit, menus and screens
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var U = DOOM.Util;
    var M = DOOM.Maps;
    var W = DOOM.Weapons;
    var Mon = DOOM.Monsters;
    var HUD = DOOM.HUD;
    var G = DOOM.GameConst;
    var Game = DOOM.Game;

    var C = U.C, R = U.RAMP;
    var css = U.paletteCSS;

    var RED = css(C(R.RED, 12));
    var DARKRED = css(C(R.BLOOD, 3));
    var BONE = css(C(R.BONE, 13));
    var GREY = css(C(R.GREY, 8));
    var YELLOW = css(C(R.YELLOW, 13));
    var BLACK = css(C(R.GREY, 0));

    // ------------------------------------------------------------ sprite list

    /** Collect everything visible this frame into the renderer's billboard list. */
    Game.prototype.buildSpriteList = function () {
        var list = [];
        var p = this.player;
        var sp = this.sprites;
        var i;

        for (i = 0; i < this.monsters.length; i++) {
            var m = this.monsters[i];
            if (m.state === Mon.S_GONE) continue;
            var vi = Mon.viewIndex(m.ang, p.x, p.y, m.x, m.y);
            var img = Mon.frameFor(m, sp, vi.view);
            if (!img) continue;
            list.push({
                x: m.x, y: m.y, img: img,
                worldH: m.def.worldH * (m.state === Mon.S_DEATH ? 0.98 : 1),
                zOff: 0, light: m.state === Mon.S_ATTACK ? 3 : 0
            });
        }

        for (i = 0; i < this.items.length; i++) {
            var it = this.items[i];
            if (it.taken) continue;
            var iimg = sp.items[it.type];
            if (!iimg) continue;
            // pickups hover and bob gently, like the originals' animated frames
            var bob = Math.sin(this.time * 2.6 + it.x * 3 + it.y * 5) * 0.035;
            list.push({
                x: it.x, y: it.y, img: iimg,
                worldH: 0.28 * (iimg.h / 22),
                zOff: 0.03 + bob,
                light: 4
            });
        }

        for (i = 0; i < this.decor.length; i++) {
            var d = this.decor[i];
            if (d.dead) continue;
            var dimg = sp.items[d.type];
            if (!dimg) continue;
            list.push({
                x: d.x, y: d.y, img: dimg,
                worldH: d.def.worldH, zOff: 0,
                light: d.def.light || 0
            });
        }

        for (i = 0; i < this.projectiles.length; i++) {
            var pr = this.projectiles[i];
            var frames = sp.missiles[pr.def.sprite];
            var pimg = frames[(this.tick >> 2) % frames.length];
            list.push({
                x: pr.x, y: pr.y, img: pimg,
                worldH: pr.def.worldH, zOff: 0.36, light: pr.def.light
            });
        }

        for (i = 0; i < this.effects.length; i++) {
            var e = this.effects[i];
            var fx = sp.fx[e.kind];
            var fi = Math.min(fx.length - 1, Math.floor(e.t / 0.055));
            var fimg = fx[fi];
            list.push({
                x: e.x, y: e.y, img: fimg,
                worldH: (fimg.h / 40) * e.scale, zOff: e.zOff,
                light: e.kind === 'explosion' || e.kind === 'sparks' ? 16 : 2
            });
        }
        return list;
    };

    // ----------------------------------------------------------------- 3D view

    Game.prototype.render3D = function () {
        var p = this.player;
        var self = this;
        var horizon = G.RENDER_H * 0.5 + p.bob - p.deathTilt * G.RENDER_H * 0.34;

        var scene = {
            map: this.map,
            px: p.x, py: p.y,
            ang: p.ang,
            fov: G.FOV,
            light: this.map.light,
            tick: this.tick,
            horizon: horizon,
            sky: this.map.sky,
            doorOpenness: function (x, y) { return self.doorOpenness(x, y); }
        };

        this.renderer.render(scene);
        this.renderer.drawSprites(scene, this.buildSpriteList());

        if (!p.dead) this.drawWeaponSprite();

        // full-screen flashes: red for damage, gold for pickups, green for rads
        if (p.damageFlash > 0) this.renderer.tint(255, 20, 10, p.damageFlash * 0.5);
        if (p.pickupFlash > 0) this.renderer.tint(255, 214, 92, p.pickupFlash * 0.35);
        if (p.radFlash > 0) this.renderer.tint(120, 230, 40, p.radFlash * 0.35);
        if (p.weaponState.flash > 0 && !p.dead) {
            this.renderer.tint(255, 230, 160, Math.min(0.22, p.weaponState.flash * 1.6));
        }
        if (this.exiting > 0) this.renderer.tint(255, 255, 255, 1 - this.exiting / 1.1);
    };

    /** Blit the first-person weapon into the render buffer with bob and recoil. */
    Game.prototype.drawWeaponSprite = function () {
        var p = this.player;
        var ws = p.weaponState;
        var img = W.viewFor(ws);
        if (!img) return;
        var buf = this.renderer.buf, bw = G.RENDER_W, bh = G.RENDER_H;

        var bobX = Math.cos(p.bobPhase) * 9;
        var bobY = Math.abs(Math.sin(p.bobPhase)) * 7;
        var x0 = Math.round((bw - img.w) / 2 + bobX);
        var y0 = Math.round(bh - img.h + bobY + ws.offset * (img.h + 30) + ws.kick);

        // The muzzle flash lights the gun itself, so drop the shade a few steps.
        var lvl = ws.flash > 0 ? 0 : 3;
        var lvlBase = lvl << 8;
        var CM = U.COLORMAP;

        for (var y = 0; y < img.h; y++) {
            var ty = y0 + y;
            if (ty < 0 || ty >= bh) continue;
            var srow = y * img.w, trow = ty * bw;
            for (var x = 0; x < img.w; x++) {
                var c = img.px[srow + x];
                if (c === 255) continue;
                var tx = x0 + x;
                if (tx < 0 || tx >= bw) continue;
                buf[trow + tx] = CM[lvlBase | c];
            }
        }
    };

    // -------------------------------------------------------------------- HUD

    Game.prototype.renderHUD = function () {
        var p = this.player;
        var wp = W.WEAPONS[p.weaponState.index];
        var bar = HUD.drawStatusBar({
            health: p.health,
            armor: p.armor,
            ammo: wp.ammo ? p.ammo[wp.ammo] : 0,
            ammoType: wp.ammo,
            ammoAll: p.ammo,
            weapons: p.weapons,
            keys: p.keys,
            face: HUD.faceState(p, this.tick)
        });

        var px = this.barPixels, CM = U.COLORMAP;
        for (var i = 0; i < bar.px.length; i++) px[i] = CM[bar.px[i]];
        this.barCtx.putImageData(this.barImage, 0, 0);
        this.ctx.drawImage(this.barCanvas, 0, 0, HUD.BAR_W, HUD.BAR_H,
            0, G.VIEW_H, G.CANVAS_W, G.BAR_H_OUT);
    };

    // ------------------------------------------------------------ composition

    Game.prototype.render = function () {
        var ctx = this.ctx;
        ctx.imageSmoothingEnabled = false;

        if (this.state === 'title' || this.state === 'skill' || this.state === 'help') {
            this.renderTitle();
            return;
        }
        if (this.state === 'intermission') { this.renderIntermission(); return; }
        if (this.state === 'victory') { this.renderVictory(); return; }

        this.render3D();

        var src = this.renderer.buf, dst = this.viewPixels;
        dst.set(src);
        this.viewCtx.putImageData(this.viewImage, 0, 0);
        ctx.drawImage(this.viewCanvas, 0, 0, G.RENDER_W, G.RENDER_H, 0, 0, G.VIEW_W, G.VIEW_H);

        this.renderHUD();
        this.renderMessages();
        this.renderCrosshair();

        if (this.state === 'dead') this.renderDeath();
        if (this.state === 'pause') this.renderPause();
    };

    Game.prototype.renderCrosshair = function () {
        if (this.player.dead) return;
        var ctx = this.ctx;
        var cx = G.VIEW_W / 2, cy = G.VIEW_H / 2;
        ctx.fillStyle = 'rgba(255,255,255,0.5)';
        ctx.fillRect(cx - 1, cy - 7, 2, 5);
        ctx.fillRect(cx - 1, cy + 3, 2, 5);
        ctx.fillRect(cx - 7, cy - 1, 5, 2);
        ctx.fillRect(cx + 3, cy - 1, 5, 2);
    };

    Game.prototype.renderMessages = function () {
        var ctx = this.ctx;
        var y = 10;
        for (var i = 0; i < this.messages.length; i++) {
            var m = this.messages[i];
            if (m.t > 4) continue;
            var alpha = m.t > 3 ? (4 - m.t) : 1;
            ctx.globalAlpha = alpha;
            HUD.drawTextCanvas(ctx, m.text, 12, y, 2, BONE, 'rgba(0,0,0,0.85)');
            ctx.globalAlpha = 1;
            y += 20;
        }
        // level name + timer, top right
        var t = Math.floor(this.levelTime);
        var clock = Math.floor(t / 60) + ':' + (t % 60 < 10 ? '0' : '') + (t % 60);
        var label = this.map.id + '  ' + clock;
        HUD.drawTextCanvas(ctx, label, G.VIEW_W - HUD.textWidth(label, 2) - 12, 10, 2,
            css(C(R.STEEL, 9)), 'rgba(0,0,0,0.8)');
    };

    // ----------------------------------------------------------------- screens

    /** The DOOM wordmark, drawn as extruded blocky letters. */
    function drawLogo(ctx, cx, y, scale, t) {
        var text = 'DOOM';
        var w = HUD.textWidth(text, scale);
        var x = cx - w / 2;
        // molten drop shadow that shimmers
        for (var d = 8; d >= 1; d--) {
            var f = d / 8;
            HUD.drawTextCanvas(ctx, text, x + d * 1.6, y + d * 1.6, scale,
                'rgba(' + Math.round(90 * f) + ',0,0,' + (0.9 - f * 0.5) + ')');
        }
        var flicker = 0.86 + Math.sin(t * 9) * 0.06 + Math.sin(t * 3.3) * 0.05;
        HUD.drawTextCanvas(ctx, text, x, y, scale,
            'rgb(' + Math.round(235 * flicker) + ',' + Math.round(60 * flicker) + ',30)');
        HUD.drawTextCanvas(ctx, text, x, y - scale, scale,
            'rgba(255,190,120,' + (0.35 + Math.sin(t * 5) * 0.1) + ')');
    }

    Game.prototype.paintBackdrop = function (hellish) {
        var ctx = this.ctx;
        var g = ctx.createLinearGradient(0, 0, 0, G.CANVAS_H);
        if (hellish) {
            g.addColorStop(0, '#1a0202');
            g.addColorStop(0.55, '#3a0a06');
            g.addColorStop(1, '#0a0000');
        } else {
            g.addColorStop(0, '#0b0b0e');
            g.addColorStop(0.6, '#1c1410');
            g.addColorStop(1, '#050505');
        }
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, G.CANVAS_W, G.CANVAS_H);

        // faint scanlines keep the CRT feel across every screen
        ctx.fillStyle = 'rgba(0,0,0,0.22)';
        for (var y = 0; y < G.CANVAS_H; y += 3) ctx.fillRect(0, y, G.CANVAS_W, 1);
    };

    var SKILL_BLURB = [
        'YOU WILL NOT BE HARMED MUCH.',
        'THE WAY THE GAME IS MEANT TO BE PLAYED.',
        'THIS IS GOING TO HURT.',
        'ARE YOU SURE THIS IS WHAT YOU WANT?'
    ];

    Game.prototype.renderTitle = function () {
        var ctx = this.ctx;
        var t = this.titleT;
        this.paintBackdrop(true);

        drawLogo(ctx, G.CANVAS_W / 2, 54, 13, t);
        HUD.drawTextCentered(ctx, 'CLASSIC 1993 EDITION', G.CANVAS_W / 2, 168, 2, css(C(R.STEEL, 8)), BLACK);

        if (this.state === 'title') {
            var pulse = 0.55 + Math.abs(Math.sin(t * 2.2)) * 0.45;
            ctx.globalAlpha = pulse;
            HUD.drawTextCentered(ctx, 'PRESS SPACE OR CLICK TO START', G.CANVAS_W / 2, 300, 3, RED, BLACK);
            ctx.globalAlpha = 1;
            HUD.drawTextCentered(ctx, 'H  -  CONTROLS AND HELP', G.CANVAS_W / 2, 352, 2, css(C(R.STEEL, 7)));
            HUD.drawTextCentered(ctx, 'KNEE-DEEP IN THE DEAD', G.CANVAS_W / 2, 420, 2, css(C(R.BLOOD, 7)));
            HUD.drawTextCentered(ctx, 'RIP AND TEAR', G.CANVAS_W / 2, 448, 2, css(C(R.BLOOD, 5)));
        } else if (this.state === 'skill') {
            HUD.drawTextCentered(ctx, 'CHOOSE YOUR SKILL LEVEL', G.CANVAS_W / 2, 220, 3, BONE, BLACK);
            for (var i = 0; i < Mon.SKILLS.length; i++) {
                var sel = i === this.menuIndex;
                var y = 270 + i * 40;
                var label = Mon.SKILLS[i].name;
                HUD.drawTextCentered(ctx, label, G.CANVAS_W / 2, y, 2,
                    sel ? YELLOW : css(C(R.STEEL, 7)), BLACK);
                if (sel) {
                    var mx = G.CANVAS_W / 2 - HUD.textWidth(label, 2) / 2 - 34;
                    HUD.drawTextCanvas(ctx, '>', mx + Math.sin(t * 8) * 4, y, 2, RED);
                }
            }
            HUD.drawTextCentered(ctx, SKILL_BLURB[this.menuIndex], G.CANVAS_W / 2, 452, 2, css(C(R.BLOOD, 8)));
            HUD.drawTextCentered(ctx, 'ARROWS TO CHOOSE  -  ENTER TO DESCEND', G.CANVAS_W / 2, 520, 1, GREY);
        } else {
            this.renderHelp();
        }
    };

    var HELP_LINES = [
        ['MOVE', 'W A S D'],
        ['TURN', 'LEFT / RIGHT ARROWS OR MOUSE'],
        ['RUN', 'SHIFT'],
        ['FIRE', 'LEFT CLICK OR CTRL'],
        ['OPEN / USE', 'SPACE'],
        ['WEAPONS', '1 FIST  2 PISTOL  3 SHOTGUN'],
        ['', '4 CHAINGUN  5 PLASMA GUN'],
        ['PAUSE', 'ESC OR P'],
        ['MUTE MUSIC', 'M'],
        ['MUTE ALL', 'N'],
        ['HELP', 'H']
    ];

    Game.prototype.renderHelp = function () {
        var ctx = this.ctx;
        HUD.drawTextCentered(ctx, 'CONTROLS', G.CANVAS_W / 2, 210, 3, BONE, BLACK);
        for (var i = 0; i < HELP_LINES.length; i++) {
            var y = 262 + i * 24;
            HUD.drawTextCanvas(ctx, HELP_LINES[i][0], 190, y, 2, YELLOW, BLACK);
            HUD.drawTextCanvas(ctx, HELP_LINES[i][1], 360, y, 2, css(C(R.STEEL, 8)), BLACK);
        }
        HUD.drawTextCentered(ctx, 'PRESS H OR ESC TO GO BACK', G.CANVAS_W / 2, 546, 2, RED);
    };

    Game.prototype.renderPause = function () {
        var ctx = this.ctx;
        ctx.fillStyle = 'rgba(0,0,0,0.7)';
        ctx.fillRect(0, 0, G.CANVAS_W, G.VIEW_H);
        HUD.drawTextCentered(ctx, 'PAUSED', G.CANVAS_W / 2, 170, 6, RED, BLACK);
        HUD.drawTextCentered(ctx, 'ESC OR P TO RESUME', G.CANVAS_W / 2, 250, 2, BONE);
        HUD.drawTextCentered(ctx, 'H FOR CONTROLS   -   M MUTES MUSIC', G.CANVAS_W / 2, 282, 2, GREY);
        HUD.drawTextCentered(ctx, 'R RESTARTS THIS LEVEL', G.CANVAS_W / 2, 314, 2, GREY);
    };

    Game.prototype.renderDeath = function () {
        var ctx = this.ctx;
        ctx.fillStyle = 'rgba(90,0,0,0.45)';
        ctx.fillRect(0, 0, G.CANVAS_W, G.VIEW_H);
        HUD.drawTextCentered(ctx, 'YOU DIED', G.CANVAS_W / 2, 150, 7, RED, BLACK);
        var pulse = 0.6 + Math.abs(Math.sin(this.time * 3)) * 0.4;
        ctx.globalAlpha = pulse;
        HUD.drawTextCentered(ctx, 'PRESS SPACE TO TRY AGAIN', G.CANVAS_W / 2, 260, 3, BONE, BLACK);
        ctx.globalAlpha = 1;
        HUD.drawTextCentered(ctx, 'ESC RETURNS TO THE TITLE SCREEN', G.CANVAS_W / 2, 320, 2, GREY);
    };

    // ------------------------------------------------------------ intermission

    /** Count a stat up on screen the way Doom's tally does. */
    function tallyValue(target, elapsed, delay, rate) {
        if (elapsed < delay) return null;
        return Math.min(target, Math.floor((elapsed - delay) * rate));
    }

    Game.prototype.renderIntermission = function () {
        var ctx = this.ctx;
        this.paintBackdrop(false);
        var t = this.interT;
        var s = this.stats;

        HUD.drawTextCentered(ctx, this.map.name, G.CANVAS_W / 2, 60, 3, RED, BLACK);
        HUD.drawTextCentered(ctx, 'FINISHED', G.CANVAS_W / 2, 104, 2, BONE);

        var pct = function (a, b) { return b === 0 ? 100 : Math.floor(a * 100 / b); };
        var rows = [
            ['KILLS', pct(s.kills, s.killsTotal), s.kills + ' / ' + s.killsTotal],
            ['ITEMS', pct(s.items, s.itemsTotal), s.items + ' / ' + s.itemsTotal],
            ['SECRET', pct(s.secrets, s.secretsTotal), s.secrets + ' / ' + s.secretsTotal]
        ];

        for (var i = 0; i < rows.length; i++) {
            var y = 190 + i * 60;
            var v = tallyValue(rows[i][1], t, 0.4 + i * 0.7, 90);
            HUD.drawTextCanvas(ctx, rows[i][0], 150, y, 3, RED, BLACK);
            if (v !== null) {
                var txt = v + '%';
                HUD.drawTextCanvas(ctx, txt, 560 - HUD.textWidth(txt, 3), y, 3, BONE, BLACK);
                HUD.drawTextCanvas(ctx, rows[i][2], 600, y + 6, 2, css(C(R.STEEL, 7)));
            }
        }

        var time = Math.floor(this.levelTime);
        var clock = Math.floor(time / 60) + ':' + (time % 60 < 10 ? '0' : '') + (time % 60);
        if (t > 2.8) {
            HUD.drawTextCanvas(ctx, 'TIME', 150, 388, 3, RED, BLACK);
            HUD.drawTextCanvas(ctx, clock, 560 - HUD.textWidth(clock, 3), 388, 3, BONE, BLACK);
            var par = this.map.par;
            HUD.drawTextCanvas(ctx, 'PAR', 150, 432, 3, RED, BLACK);
            var ptxt = Math.floor(par / 60) + ':' + (par % 60 < 10 ? '0' : '') + (par % 60);
            HUD.drawTextCanvas(ctx, ptxt, 560 - HUD.textWidth(ptxt, 3), 432, 3, BONE, BLACK);
        }

        if (t > 3.6) {
            var last = this.levelIndex + 1 >= M.count;
            var msg = last ? 'PRESS SPACE TO FACE THE END' : 'ENTERING NEXT LEVEL...';
            var pulse = 0.55 + Math.abs(Math.sin(t * 3)) * 0.45;
            ctx.globalAlpha = pulse;
            HUD.drawTextCentered(ctx, msg, G.CANVAS_W / 2, 520, 3, YELLOW, BLACK);
            ctx.globalAlpha = 1;
            if (!last) {
                HUD.drawTextCentered(ctx, M.DEFS[this.levelIndex + 1].name, G.CANVAS_W / 2, 560, 2, css(C(R.STEEL, 8)));
            }
        }
    };

    Game.prototype.renderVictory = function () {
        var ctx = this.ctx;
        this.paintBackdrop(true);
        var t = this.titleT;

        drawLogo(ctx, G.CANVAS_W / 2, 40, 10, t);
        HUD.drawTextCentered(ctx, 'THE ANOMALY IS DEAD', G.CANVAS_W / 2, 170, 3, RED, BLACK);

        var lines = [
            'THE CYBERDEMON FALLS. THE PENTAGRAM GOES DARK.',
            '',
            'YOU STEP THROUGH THE TELEPORTER AND PHOBOS IS',
            'BEHIND YOU AT LAST. BUT THE GATE SWUNG BOTH WAYS,',
            'AND SOMETHING IS ALREADY WALKING BACK.',
            '',
            'FOR NOW: YOU WON.'
        ];
        for (var i = 0; i < lines.length; i++) {
            // typewriter reveal, one line at a time
            if (t < 0.5 + i * 0.45) break;
            HUD.drawTextCentered(ctx, lines[i], G.CANVAS_W / 2, 240 + i * 30, 2, BONE, BLACK);
        }

        if (t > 4) {
            var s = this.stats;
            HUD.drawTextCentered(ctx, 'FINAL KILLS  ' + s.kills + ' / ' + s.killsTotal,
                G.CANVAS_W / 2, 470, 2, css(C(R.STEEL, 8)));
            var pulse = 0.55 + Math.abs(Math.sin(t * 3)) * 0.45;
            ctx.globalAlpha = pulse;
            HUD.drawTextCentered(ctx, 'PRESS SPACE FOR THE TITLE SCREEN', G.CANVAS_W / 2, 520, 2, YELLOW, BLACK);
            ctx.globalAlpha = 1;
        }
    };

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


/* --- File: 13_main.js --- */

/* ==========================================================================
 * DOOM :: main.js -- widget mount, DOM bootstrap, event handling, main loop
 * ========================================================================== */
(function (DOOM) {
    'use strict';

    var G = DOOM.GameConst;
    var Sound = DOOM.Sound;
    var Music = DOOM.Music;
    var M = DOOM.Maps;

    function mount(rootElement, options) {
        options = options || {};
        if (typeof rootElement === 'string') {
            rootElement = document.querySelector(rootElement);
        }
        if (!rootElement) {
            console.error('[DOOM] Mount root element not found');
            return null;
        }

        // Clean container
        rootElement.innerHTML = '';
        rootElement.style.position = 'relative';
        rootElement.style.width = '100%';
        rootElement.style.height = '100%';
        rootElement.style.minHeight = '720px';
        rootElement.style.backgroundColor = '#0b0b0f';
        rootElement.style.color = '#e0e0e0';
        rootElement.style.fontFamily = 'monospace, sans-serif';
        rootElement.style.display = 'flex';
        rootElement.style.flexDirection = 'column';
        rootElement.style.alignItems = 'center';
        rootElement.style.justifyContent = 'flex-start';
        rootElement.style.userSelect = 'none';
        rootElement.style.overflow = 'hidden';

        // Container wrapper for CRT/Doom aesthetic
        var wrapper = document.createElement('div');
        wrapper.className = 'doom-widget-container';
        wrapper.style.position = 'relative';
        wrapper.style.display = 'flex';
        wrapper.style.flexDirection = 'column';
        wrapper.style.alignItems = 'center';
        wrapper.style.justifyContent = 'center';
        wrapper.style.width = '100%';
        wrapper.style.maxWidth = '960px';
        wrapper.style.padding = '8px';
        wrapper.style.boxSizing = 'border-box';

        // Header bar with status and controls
        var header = document.createElement('div');
        header.style.display = 'flex';
        header.style.justifyContent = 'space-between';
        header.style.alignItems = 'center';
        header.style.width = '100%';
        header.style.maxWidth = '800px';
        header.style.padding = '6px 12px';
        header.style.marginBottom = '6px';
        header.style.backgroundColor = '#16161e';
        header.style.border = '1px solid #2a2a38';
        header.style.borderRadius = '4px';
        header.style.fontSize = '12px';
        header.style.boxSizing = 'border-box';

        header.innerHTML = '<div style="display:flex;align-items:center;gap:8px;">' +
            '<span style="color:#d32f2f;font-weight:bold;letter-spacing:1px;font-size:14px;">DOOM</span>' +
            '<span style="color:#888;">(1993) • Pure JS Raycaster</span>' +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:12px;" id="doom-header-controls">' +
            '<button id="doom-btn-audio" style="background:#222;color:#aaa;border:1px solid #444;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:11px;">🔊 Sound: ON</button>' +
            '<button id="doom-btn-music" style="background:#222;color:#aaa;border:1px solid #444;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:11px;">🎵 Music: ON</button>' +
            '<button id="doom-btn-restart" style="background:#222;color:#aaa;border:1px solid #444;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:11px;">↺ Restart</button>' +
            '</div>';

        wrapper.appendChild(header);

        // Canvas frame with CRT bezel effect
        var frame = document.createElement('div');
        frame.style.position = 'relative';
        frame.style.width = '100%';
        frame.style.maxWidth = '800px';
        frame.style.aspectRatio = '4 / 3';
        frame.style.maxHeight = '600px';
        frame.style.backgroundColor = '#000';
        frame.style.boxShadow = '0 8px 24px rgba(0,0,0,0.8), 0 0 0 2px #282834';
        frame.style.borderRadius = '4px';
        frame.style.overflow = 'hidden';
        frame.style.cursor = 'crosshair';

        var canvas = document.createElement('canvas');
        canvas.width = G.CANVAS_W;
        canvas.height = G.CANVAS_H;
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.display = 'block';
        canvas.style.imageRendering = 'pixelated';
        canvas.tabIndex = 1; // Make focusable
        frame.appendChild(canvas);

        wrapper.appendChild(frame);

        // Footer instructions / controls help
        var footer = document.createElement('div');
        footer.style.display = 'flex';
        footer.style.justifyContent = 'center';
        footer.style.alignItems = 'center';
        footer.style.flexWrap = 'wrap';
        footer.style.gap = '14px';
        footer.style.width = '100%';
        footer.style.maxWidth = '800px';
        footer.style.padding = '8px 12px';
        footer.style.marginTop = '6px';
        footer.style.color = '#777';
        footer.style.fontSize = '11px';
        footer.style.textAlign = 'center';

        footer.innerHTML = '<span><b style="color:#aaa;">WASD / Arrows:</b> Move</span>' +
            '<span><b style="color:#aaa;">Mouse / Left-Right:</b> Turn</span>' +
            '<span><b style="color:#aaa;">Left Click / Ctrl:</b> Fire</span>' +
            '<span><b style="color:#aaa;">Space:</b> Open / Use</span>' +
            '<span><b style="color:#aaa;">1-5:</b> Weapons</span>' +
            '<span><b style="color:#aaa;">Shift:</b> Run</span>' +
            '<span><b style="color:#aaa;">Esc:</b> Pause</span>';

        wrapper.appendChild(footer);
        rootElement.appendChild(wrapper);

        // Instantiate DOOM Game
        var game = new DOOM.Game(canvas);

        // Setup audio buttons
        var btnAudio = header.querySelector('#doom-btn-audio');
        var btnMusic = header.querySelector('#doom-btn-music');
        var btnRestart = header.querySelector('#doom-btn-restart');

        if (btnAudio) {
            btnAudio.onclick = function (e) {
                e.stopPropagation();
                game.soundMuted = !game.soundMuted;
                Sound.muted = game.soundMuted;
                btnAudio.innerText = game.soundMuted ? '🔇 Sound: OFF' : '🔊 Sound: ON';
                btnAudio.style.color = game.soundMuted ? '#e57373' : '#aaa';
            };
        }

        if (btnMusic) {
            btnMusic.onclick = function (e) {
                e.stopPropagation();
                game.musicMuted = !game.musicMuted;
                Music.muted = game.musicMuted;
                if (game.musicMuted) {
                    Music.stop();
                } else if (game.state === 'play' && game.map) {
                    Music.start(game.map.music);
                }
                btnMusic.innerText = game.musicMuted ? '🔇 Music: OFF' : '🎵 Music: ON';
                btnMusic.style.color = game.musicMuted ? '#e57373' : '#aaa';
            };
        }

        if (btnRestart) {
            btnRestart.onclick = function (e) {
                e.stopPropagation();
                game.state = 'title';
                game.titleT = 0;
                Music.stop();
            };
        }

        // Unlock audio on first user gesture
        function unlockAudio() {
            try {
                Sound.init();
                Sound.resume();
            } catch (err) {
                console.warn('[DOOM] Audio init warning:', err);
            } finally {
                window.removeEventListener('click', unlockAudio);
                window.removeEventListener('keydown', unlockAudio);
            }
        }
        window.addEventListener('click', unlockAudio);
        window.addEventListener('keydown', unlockAudio);

        // Input Bindings
        function onKeyDown(e) {
            // Prevent scrolling on arrows/space if canvas or wrapper is focused
            if (['Space', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Tab'].indexOf(e.code) !== -1) {
                if (document.activeElement === canvas || rootElement.contains(document.activeElement)) {
                    e.preventDefault();
                }
            }
            game.keysDown[e.code] = true;

            // Handle title/menu/pause keys directly
            if (game.state === 'title') {
                if (e.code === 'KeyH') {
                    game.state = 'help';
                    Sound.play('switchFlip', 0.8);
                } else if (e.code === 'Space' || e.code === 'Enter') {
                    game.state = 'skill';
                    game.menuIndex = 1; // Hurt me plenty
                    Sound.play('switchFlip', 0.8);
                }
            } else if (game.state === 'help') {
                if (e.code === 'KeyH' || e.code === 'Escape' || e.code === 'Space' || e.code === 'Enter') {
                    game.state = 'title';
                    Sound.play('switchFlip', 0.8);
                }
            } else if (game.state === 'skill') {
                if (e.code === 'ArrowUp' || e.code === 'KeyW') {
                    game.menuIndex = (game.menuIndex + 3) % 4;
                    Sound.play('swish', 0.6);
                } else if (e.code === 'ArrowDown' || e.code === 'KeyS') {
                    game.menuIndex = (game.menuIndex + 1) % 4;
                    Sound.play('swish', 0.6);
                } else if (e.code === 'Space' || e.code === 'Enter') {
                    game.startGame(game.menuIndex);
                } else if (e.code === 'Escape') {
                    game.state = 'title';
                }
            } else if (game.state === 'dead') {
                if (e.code === 'Space' || e.code === 'Enter') {
                    game.startGame(game.skill);
                } else if (e.code === 'Escape') {
                    game.state = 'title';
                    Music.stop();
                }
            } else if (game.state === 'intermission') {
                if (e.code === 'Space' || e.code === 'Enter') {
                    if (game.interT > 0.8) {
                        if (game.levelIndex + 1 >= M.count) {
                            game.state = 'victory';
                            game.titleT = 0;
                            Music.start('victory');
                        } else {
                            game.loadLevel(game.levelIndex + 1);
                            game.state = 'play';
                        }
                    }
                }
            } else if (game.state === 'victory') {
                if (e.code === 'Space' || e.code === 'Enter') {
                    if (game.titleT > 3) {
                        game.state = 'title';
                        Music.stop();
                    }
                }
            } else if (game.state === 'play') {
                if (e.code === 'Escape' || e.code === 'KeyP') {
                    game.state = 'pause';
                    Sound.play('switchFlip', 0.8);
                } else if (e.code === 'Space') {
                    game.useAction();
                } else if (e.code === 'KeyM') {
                    if (btnMusic) btnMusic.click();
                }
            } else if (game.state === 'pause') {
                if (e.code === 'Escape' || e.code === 'KeyP' || e.code === 'Space' || e.code === 'Enter') {
                    game.state = 'play';
                    Sound.play('switchFlip', 0.8);
                }
            }
        }

        function onKeyUp(e) {
            game.keysDown[e.code] = false;
        }

        function onMouseDown(e) {
            canvas.focus();
            if (e.button === 0) { // Left click
                if (game.state === 'title') {
                    game.state = 'skill';
                    game.menuIndex = 1;
                    Sound.play('switchFlip', 0.8);
                } else if (game.state === 'help') {
                    game.state = 'title';
                    Sound.play('switchFlip', 0.8);
                } else if (game.state === 'skill') {
                    game.startGame(game.menuIndex);
                } else if (game.state === 'dead') {
                    game.startGame(game.skill);
                } else if (game.state === 'intermission') {
                    if (game.interT > 0.8) {
                        if (game.levelIndex + 1 >= M.count) {
                            game.state = 'victory';
                            game.titleT = 0;
                            Music.start('victory');
                        } else {
                            game.loadLevel(game.levelIndex + 1);
                            game.state = 'play';
                        }
                    }
                } else if (game.state === 'victory') {
                    if (game.titleT > 3) {
                        game.state = 'title';
                        Music.stop();
                    }
                } else if (game.state === 'play') {
                    game.mouseFire = true;
                } else if (game.state === 'pause') {
                    game.state = 'play';
                }
            }
        }

        function onMouseUp(e) {
            if (e.button === 0) {
                game.mouseFire = false;
            }
        }

        // Mouse look support
        var lastMouseX = null;
        function onMouseMove(e) {
            if (game.state === 'play' && game.player && !game.player.dead) {
                if (document.pointerLockElement === canvas) {
                    var movementX = e.movementX || e.mozMovementX || e.webkitMovementX || 0;
                    game.player.ang += movementX * 0.0035;
                } else if (lastMouseX !== null && (e.buttons & 1)) {
                    var dx = e.clientX - lastMouseX;
                    game.player.ang += dx * 0.005;
                }
                lastMouseX = e.clientX;
            }
        }

        // Optional pointer lock on click during play
        function onDblClick() {
            if (game.state === 'play') {
                if (canvas.requestPointerLock) canvas.requestPointerLock();
            }
        }

        window.addEventListener('keydown', onKeyDown);
        window.addEventListener('keyup', onKeyUp);
        canvas.addEventListener('mousedown', onMouseDown);
        window.addEventListener('mouseup', onMouseUp);
        canvas.addEventListener('mousemove', onMouseMove);
        canvas.addEventListener('dblclick', onDblClick);

        // Main game animation loop
        var lastTime = performance.now();
        var running = true;

        function loop(now) {
            if (!running) return;
            var dt = (now - lastTime) / 1000;
            lastTime = now;

            // Cap dt to prevent huge leaps on tab change
            if (dt > 0.1) dt = 0.1;
            if (dt < 0.001) dt = 0.001;

            game.update(dt);
            game.render();

            requestAnimationFrame(loop);
        }

        requestAnimationFrame(loop);

        return {
            game: game,
            destroy: function () {
                running = false;
                Music.stop();
                window.removeEventListener('keydown', onKeyDown);
                window.removeEventListener('keyup', onKeyUp);
                canvas.removeEventListener('mousedown', onMouseDown);
                window.removeEventListener('mouseup', onMouseUp);
                canvas.removeEventListener('mousemove', onMouseMove);
                canvas.removeEventListener('dblclick', onDblClick);
                window.removeEventListener('click', unlockAudio);
                window.removeEventListener('keydown', unlockAudio);
                rootElement.innerHTML = '';
            }
        };
    }

    DOOM.mount = mount;

    // Auto-mount if #root exists on load
    if (typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function () {
                var root = document.getElementById('root');
                if (root) mount(root);
            });
        } else {
            var root = document.getElementById('root');
            if (root) mount(root);
        }
    }

})(typeof DOOM !== 'undefined' ? DOOM
    : (typeof globalThis !== 'undefined' ? (globalThis.DOOM = globalThis.DOOM || {}) : this));


window.DOOM = DOOM;
})();
