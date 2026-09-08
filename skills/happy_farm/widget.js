/* Happy Farm — Cozy Farming Simulation Widget (v0.1.0)
 * Fully interactive Canvas 2D + Glassmorphism UI, Web Audio SFX,
 * Livestock, Day/Night/Weather Cycle, Orders, Economy, and Cloud-backed Persistence.
 */
(function () {
    'use strict';

    var root = document.getElementById('root');
    if (!root) return;

    var API_BASE = '/api/extensions/happy_farm';

    // Default configuration (hydrated dynamically from /api/extensions/happy_farm/config)
    var CONFIG = {
        gridCols: 8,
        gridRows: 6,
        tileSize: 64,
        initialCoins: 150,
        crops: {
            wheat: { id: 'wheat', name: 'Wheat', nameRu: 'Пшеница', icon: '🌾', growthSec: 12, seedCost: 5, sellPrice: 12, xp: 5, minLevel: 1, color: '#eab308' },
            carrot: { id: 'carrot', name: 'Carrot', nameRu: 'Морковь', icon: '🥕', growthSec: 24, seedCost: 10, sellPrice: 26, xp: 12, minLevel: 1, color: '#f97316' },
            tomato: { id: 'tomato', name: 'Tomato', nameRu: 'Помидор', icon: '🍅', growthSec: 40, seedCost: 20, sellPrice: 55, xp: 25, minLevel: 2, color: '#ef4444' },
            strawberry: { id: 'strawberry', name: 'Strawberry', nameRu: 'Клубника', icon: '🍓', growthSec: 60, seedCost: 35, sellPrice: 100, xp: 45, minLevel: 3, color: '#ec4899' },
            corn: { id: 'corn', name: 'Corn', nameRu: 'Кукуруза', icon: '🌽', growthSec: 90, seedCost: 60, sellPrice: 175, xp: 80, minLevel: 4, color: '#facc15' },
            watermelon: { id: 'watermelon', name: 'Watermelon', nameRu: 'Арбуз', icon: '🍉', growthSec: 130, seedCost: 100, sellPrice: 310, xp: 140, minLevel: 5, color: '#22c55e' },
            pumpkin: { id: 'pumpkin', name: 'Magic Pumpkin', nameRu: 'Тыква', icon: '🎃', growthSec: 180, seedCost: 180, sellPrice: 580, xp: 260, minLevel: 6, color: '#d97706' }
        },
        animals: {
            chicken: { id: 'chicken', name: 'Chicken', nameRu: 'Курица', icon: '🐔', cost: 120, product: 'egg', productName: 'Egg', productNameRu: 'Яйцо', productIcon: '🥚', produceSec: 35, productSell: 45, xp: 20, minLevel: 2 },
            cow: { id: 'cow', name: 'Cow', nameRu: 'Корова', icon: '🐮', cost: 350, product: 'milk', productName: 'Fresh Milk', productNameRu: 'Молоко', productIcon: '🥛', produceSec: 65, productSell: 130, xp: 55, minLevel: 3 },
            sheep: { id: 'sheep', name: 'Sheep', nameRu: 'Овца', icon: '🐑', cost: 650, product: 'wool', productName: 'Soft Wool', productNameRu: 'Шерсть', productIcon: '🧶', produceSec: 95, productSell: 260, xp: 110, minLevel: 5 }
        },
        tools: [
            { id: 'hand', name: 'Hand', nameRu: 'Рука', icon: '🖐', desc: 'Inspect & harvest' },
            { id: 'hoe', name: 'Hoe', nameRu: 'Тяпка', icon: '⛏️', desc: 'Till grassy soil' },
            { id: 'water', name: 'Water Can', nameRu: 'Лейка', icon: '💧', desc: 'Water dry crops' },
            { id: 'scythe', name: 'Scythe', nameRu: 'Серп', icon: '🌾', desc: 'Mass harvest 3x3 ready crops' },
            { id: 'seed', name: 'Seed', nameRu: 'Семена', icon: '🌱', desc: 'Plant selected crop' },
            { id: 'fertilizer', name: 'Fertilizer', nameRu: 'Удобрение', icon: '✨', cost: 15, desc: 'Instant growth boost (-50% time)' },
            { id: 'feed', name: 'Feed', nameRu: 'Корм', icon: '🌾', cost: 8, desc: 'Feed livestock to produce' }
        ],
        upgrades: {
            sprinkler: { id: 'sprinkler', name: 'Auto-Sprinkler', nameRu: 'Автополивалка', icon: '🚿', cost: 400, minLevel: 4, desc: 'Waters 3x3 surrounding plots automatically' },
            scarecrow: { id: 'scarecrow', name: 'Scarecrow', nameRu: 'Пугало', icon: '🪵', cost: 250, minLevel: 3, desc: 'Gives +20% extra harvest yields' },
            silo: { id: 'silo', name: 'Big Silo', nameRu: 'Большой амбар', icon: '🏡', cost: 500, minLevel: 3, desc: 'Expands storage capacity' }
        },
        xpLevels: [0, 50, 150, 350, 750, 1400, 2400, 4000, 6500, 10000, 15000]
    };

    // --- SOUND SYNTHESIZER (Web Audio API) ---
    var AudioSys = {
        ctx: null,
        muted: false,
        init: function () {
            if (!this.ctx) {
                var AudioContext = window.AudioContext || window.webkitAudioContext;
                if (AudioContext) {
                    this.ctx = new AudioContext();
                }
            }
            if (this.ctx && this.ctx.state === 'suspended') {
                this.ctx.resume();
            }
        },
        beep: function (freq, type, duration, gainStart, gainEnd, detune) {
            if (this.muted) return;
            this.init();
            if (!this.ctx) return;
            try {
                var osc = this.ctx.createOscillator();
                var gain = this.ctx.createGain();
                osc.type = type || 'sine';
                osc.frequency.setValueAtTime(freq, this.ctx.currentTime);
                if (detune) osc.detune.setValueAtTime(detune, this.ctx.currentTime);
                gain.gain.setValueAtTime(gainStart !== undefined ? gainStart : 0.15, this.ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(gainEnd || 0.001, this.ctx.currentTime + duration);
                osc.connect(gain);
                gain.connect(this.ctx.destination);
                osc.start();
                osc.stop(this.ctx.currentTime + duration);
            } catch (e) {}
        },
        playTill: function () {
            this.beep(120, 'triangle', 0.12, 0.2, 0.01);
            this.beep(80, 'sine', 0.08, 0.25, 0.01);
        },
        playWater: function () {
            this.beep(480, 'sine', 0.18, 0.15, 0.01);
            this.beep(640, 'triangle', 0.14, 0.1, 0.01);
        },
        playPlant: function () {
            this.beep(320, 'sine', 0.1, 0.18, 0.01);
            this.beep(420, 'sine', 0.15, 0.15, 0.01);
        },
        playHarvest: function () {
            var self = this;
            [523.25, 659.25, 783.99, 1046.50].forEach(function (f, i) {
                setTimeout(function () {
                    self.beep(f, 'triangle', 0.2, 0.2, 0.001);
                }, i * 40);
            });
        },
        playCoin: function () {
            this.beep(987.77, 'sine', 0.08, 0.2, 0.01);
            var self = this;
            setTimeout(function () {
                self.beep(1318.51, 'sine', 0.18, 0.2, 0.001);
            }, 60);
        },
        playLevelUp: function () {
            var self = this;
            var notes = [440, 554.37, 659.25, 880, 1108.73, 1318.51];
            notes.forEach(function (f, i) {
                setTimeout(function () {
                    self.beep(f, 'triangle', 0.25, 0.25, 0.001);
                }, i * 60);
            });
        },
        playAnimal: function (type) {
            if (type === 'chicken') {
                this.beep(600, 'sawtooth', 0.08, 0.1, 0.01);
                this.beep(750, 'sawtooth', 0.12, 0.15, 0.01);
            } else if (type === 'cow') {
                this.beep(130, 'triangle', 0.4, 0.25, 0.01);
                this.beep(110, 'sine', 0.5, 0.2, 0.01);
            } else if (type === 'sheep') {
                this.beep(260, 'sawtooth', 0.25, 0.15, 0.01);
                this.beep(220, 'sine', 0.3, 0.12, 0.01);
            }
        },
        playClick: function () {
            this.beep(400, 'triangle', 0.05, 0.08, 0.01);
        }
    };

    // --- GAME STATE ---
    var State = {
        coins: CONFIG.initialCoins,
        level: 1,
        xp: 0,
        activeTool: 'hand',
        selectedCrop: 'wheat',
        timeSpeed: 1,
        dayTime: 360, // minutes in day (0 to 1440). 360 = 6:00 AM
        dayCount: 1,
        weather: 'sunny',
        grid: [],
        inventory: {
            seeds: { wheat: 6, carrot: 3, tomato: 0, strawberry: 0, corn: 0, watermelon: 0, pumpkin: 0 },
            produce: { wheat: 0, carrot: 0, tomato: 0, strawberry: 0, corn: 0, watermelon: 0, pumpkin: 0, egg: 0, milk: 0, wool: 0 },
            fertilizer: 2,
            feed: 4
        },
        animals: [],
        upgrades: {
            sprinklers: [],
            scarecrows: [],
            silo: 0
        },
        orders: [],
        particles: [],
        popups: [],
        stats: {
            cropsHarvested: 0,
            coinsEarned: 0,
            ordersCompleted: 0
        },
        isSaving: false,
        loadFailed: false,
        lastSaveTime: 0,

        initGrid: function () {
            this.grid = [];
            for (var r = 0; r < CONFIG.gridRows; r++) {
                for (var c = 0; c < CONFIG.gridCols; c++) {
                    var isTilled = (r >= 1 && r <= 4 && c >= 1 && c <= 6);
                    this.grid.push({
                        col: c,
                        row: r,
                        tilled: isTilled,
                        watered: isTilled && (c % 2 === 0),
                        crop: isTilled && c === 2 && r === 2 ? 'wheat' : null,
                        stage: isTilled && c === 2 && r === 2 ? 1 : 0,
                        growthProgress: isTilled && c === 2 && r === 2 ? 0.3 : 0,
                        fertilized: false,
                        sprinkler: false,
                        scarecrow: false
                    });
                }
            }
        },

        initAnimals: function () {
            this.animals = [
                { id: 'c1', type: 'chicken', x: 50, y: 320, targetX: 60, targetY: 330, fed: true, progress: 0.4, produceReady: false },
                { id: 'c2', type: 'chicken', x: 80, y: 340, targetX: 90, targetY: 340, fed: false, progress: 0, produceReady: false },
                { id: 'cow1', type: 'cow', x: 550, y: 320, targetX: 540, targetY: 310, fed: true, progress: 0.2, produceReady: false }
            ];
        },

        initOrders: function () {
            this.orders = [
                { id: 1, villager: 'Grandma Clara', avatar: '👵', text: 'Need fresh wheat for morning bread!', req: { wheat: 4 }, rewardCoins: 75, rewardXp: 30, completed: false },
                { id: 2, villager: 'Chef Marco', avatar: '👨‍🍳', text: 'Crispy carrots and fresh eggs for the royal stew!', req: { carrot: 3, egg: 2 }, rewardCoins: 180, rewardXp: 65, completed: false },
                { id: 3, villager: 'Farmer Pete', avatar: '🧑‍🌾', text: 'Tomatoes & milk for the weekly farm festival!', req: { tomato: 4, milk: 1 }, rewardCoins: 360, rewardXp: 120, completed: false }
            ];
        },

        addXP: function (amount) {
            this.xp += amount;
            var maxLvl = CONFIG.xpLevels.length;
            var nextXp = CONFIG.xpLevels[this.level] || 999999;
            while (this.xp >= nextXp && this.level < maxLvl) {
                this.level++;
                this.coins += this.level * 50;
                AudioSys.playLevelUp();
                this.spawnConfetti();
                var viewW = (canvas.width / dpr) || 600;
                var viewH = (canvas.height / dpr) || 400;
                addFloatingText(viewW / 2, viewH / 2 - 40, 'LEVEL UP! Level ' + this.level + ' 🎉 (+' + (this.level * 50) + '🪙)', '#facc15', 26);
                nextXp = CONFIG.xpLevels[this.level] || 999999;
            }
            updateHUD();
        },

        spawnConfetti: function () {
            var viewW = (canvas.width / dpr) || 600;
            var viewH = (canvas.height / dpr) || 400;
            var colors = ['#f43f5e', '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#ffffff'];
            for (var i = 0; i < 60; i++) {
                this.particles.push({
                    x: viewW / 2 + (Math.random() - 0.5) * 200,
                    y: viewH / 2 + (Math.random() - 0.5) * 100,
                    vx: (Math.random() - 0.5) * 8,
                    vy: (Math.random() - 0.8) * 8 - 3,
                    size: Math.random() * 6 + 4,
                    color: colors[Math.floor(Math.random() * colors.length)],
                    life: 1.0,
                    decay: Math.random() * 0.015 + 0.01,
                    rotation: Math.random() * 360,
                    vRot: (Math.random() - 0.5) * 15
                });
            }
        },

        saveToBackend: function () {
            var self = this;
            if (self.isSaving || self.loadFailed) return;
            self.isSaving = true;

            var payload = {
                coins: self.coins,
                level: self.level,
                xp: self.xp,
                inventory: self.inventory,
                grid: self.grid,
                animals: self.animals,
                upgrades: self.upgrades,
                orders: self.orders,
                stats: self.stats,
                dayCount: self.dayCount,
                dayTime: self.dayTime,
                savedAt: Date.now()
            };

            fetch(API_BASE + '/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(function (res) {
                return res.json();
            }).then(function (data) {
                self.isSaving = false;
                if (data && data.ok) {
                    self.lastSaveTime = Date.now();
                } else {
                    addFloatingText((canvas.width / dpr) / 2, 70, '⚠️ Сохранение не удалось', '#f87171', 16);
                }
            }).catch(function () {
                self.isSaving = false;
                addFloatingText((canvas.width / dpr) / 2, 70, '⚠️ Нет связи с хранилищем', '#f87171', 16);
            });
        },

        loadFromBackend: function (callback) {
            var self = this;
            fetch(API_BASE + '/config').then(function (res) {
                return res.json();
            }).then(function (data) {
                if (data && data.ok) {
                    // Hydrate Config if available
                    if (data.config) {
                        if (data.config.crops) CONFIG.crops = data.config.crops;
                        if (data.config.animals) CONFIG.animals = data.config.animals;
                        if (data.config.xp_levels) CONFIG.xpLevels = data.config.xp_levels;
                    }

                    // Hydrate Cloud Save if available
                    if (data.has_cloud_save && data.cloud_save && typeof data.cloud_save === 'object') {
                        var save = data.cloud_save;
                        self.coins = typeof save.coins === 'number' ? Math.max(0, save.coins) : self.coins;
                        self.level = typeof save.level === 'number' ? Math.max(1, save.level) : self.level;
                        self.xp = typeof save.xp === 'number' ? Math.max(0, save.xp) : self.xp;
                        if (save.inventory && typeof save.inventory === 'object') {
                            self.inventory.seeds = Object.assign({}, self.inventory.seeds, save.inventory.seeds || {});
                            self.inventory.produce = Object.assign({}, self.inventory.produce, save.inventory.produce || {});
                            self.inventory.fertilizer = Number.isFinite(save.inventory.fertilizer) ? Math.max(0, save.inventory.fertilizer) : self.inventory.fertilizer;
                            self.inventory.feed = Number.isFinite(save.inventory.feed) ? Math.max(0, save.inventory.feed) : self.inventory.feed;
                        }
                        if (Array.isArray(save.grid) && save.grid.length === CONFIG.gridCols * CONFIG.gridRows) self.grid = save.grid;
                        else self.initGrid();
                        if (Array.isArray(save.animals)) self.animals = save.animals;
                        else self.initAnimals();
                        if (save.upgrades && typeof save.upgrades === 'object') {
                            self.upgrades.sprinklers = Array.isArray(save.upgrades.sprinklers) ? save.upgrades.sprinklers : [];
                            self.upgrades.scarecrows = Array.isArray(save.upgrades.scarecrows) ? save.upgrades.scarecrows : [];
                            self.upgrades.silo = Number.isFinite(save.upgrades.silo) ? Math.max(0, save.upgrades.silo) : 0;
                        }
                        if (Array.isArray(save.orders) && save.orders.length) self.orders = save.orders;
                        else self.initOrders();
                        self.stats = save.stats && typeof save.stats === 'object' ? save.stats : self.stats;
                        self.dayCount = typeof save.dayCount === 'number' ? Math.max(1, save.dayCount) : self.dayCount;
                        self.dayTime = typeof save.dayTime === 'number' ? Math.max(0, Math.min(1439, save.dayTime)) : self.dayTime;

                        if (save.savedAt) {
                            var elapsedSec = Math.min(3600, Math.floor((Date.now() - save.savedAt) / 1000));
                            if (elapsedSec > 5) self.processOfflineProgress(elapsedSec);
                        }
                    } else {
                        self.initGrid();
                        self.initAnimals();
                        self.initOrders();
                    }
                } else {
                    self.initGrid();
                    self.initAnimals();
                    self.initOrders();
                }
                if (callback) callback();
            }).catch(function () {
                self.loadFailed = true;
                self.initGrid();
                self.initAnimals();
                self.initOrders();
                addFloatingText((canvas.width / dpr) / 2, 70, '⚠️ Не удалось загрузить сохранение — автосохранение приостановлено', '#f87171', 14);
                if (callback) callback();
            });
        },

        processOfflineProgress: function (seconds) {
            for (var i = 0; i < this.grid.length; i++) {
                var tile = this.grid[i];
                if (tile.crop && tile.watered && tile.stage < 3) {
                    var cropDef = CONFIG.crops[tile.crop];
                    if (cropDef) {
                        var addedGrowth = seconds / (cropDef.growth_sec || cropDef.growthSec || 20);
                        tile.growthProgress += addedGrowth;
                        if (tile.growthProgress >= 1.0) {
                            tile.stage = 3;
                            tile.growthProgress = 1.0;
                        } else if (tile.growthProgress >= 0.5) {
                            tile.stage = 2;
                        } else {
                            tile.stage = 1;
                        }
                    }
                }
            }
        }
    };

    // --- DOM STRUCTURE CREATION ---
    root.innerHTML = '';
    var container = document.createElement('div');
    container.id = 'happy-farm-app';
    container.style.cssText = 'position:relative;width:100%;min-height:760px;height:780px;background:#131d15;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;flex-direction:column;overflow:hidden;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.1);user-select:none;';

    // 1. Top Header & HUD
    var header = document.createElement('div');
    header.style.cssText = 'height:54px;background:linear-gradient(180deg,rgba(30,45,35,0.95),rgba(20,30,22,0.9));border-bottom:1px solid rgba(255,255,255,0.12);display:flex;align-items:center;justify-content:space-between;padding:0 16px;z-index:20;backdrop-filter:blur(8px);';
    header.innerHTML = [
        '<div style="display:flex;align-items:center;gap:12px;">',
        '  <div style="font-size:22px;line-height:1;">🌻</div>',
        '  <div>',
        '    <div style="font-weight:700;font-size:16px;color:#fef08a;display:flex;align-items:center;gap:6px;">Happy Farm <span style="font-size:11px;padding:2px 6px;border-radius:6px;background:rgba(234,179,8,0.2);color:#fde047;border:1px solid rgba(234,179,8,0.3);">v0.1.0</span></div>',
        '    <div id="farm-clock" style="font-size:11px;color:#94a3b8;">День 1 · 06:00 AM · ☀️ Солнечно</div>',
        '  </div>',
        '</div>',
        '<div style="display:flex;align-items:center;gap:18px;">',
        '  <div style="display:flex;align-items:center;gap:8px;background:rgba(0,0,0,0.35);padding:4px 12px;border-radius:20px;border:1px solid rgba(255,255,255,0.08);">' +
        '    <span style="font-size:16px;">🪙</span><span id="stat-coins" style="font-weight:700;color:#facc15;font-size:15px;">150</span>' +
        '  </div>',
        '  <div style="display:flex;align-items:center;gap:8px;min-width:140px;">' +
        '    <div style="display:flex;flex-direction:column;width:100%;">' +
        '      <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:2px;">' +
        '        <span style="color:#60a5fa;font-weight:600;">Ур <span id="stat-lvl">1</span></span>' +
        '        <span id="stat-xp-txt" style="color:#94a3b8;">0/50 XP</span>' +
        '      </div>' +
        '      <div style="height:6px;width:100%;background:rgba(255,255,255,0.1);border-radius:4px;overflow:hidden;">' +
        '        <div id="stat-xp-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#3b82f6,#60a5fa);transition:width 0.3s ease;"></div>' +
        '      </div>' +
        '    </div>' +
        '  </div>',
        '  <div style="display:flex;gap:6px;">',
        '    <button id="btn-speed" title="Game Speed" style="background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);color:#cbd5e1;padding:4px 8px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;">⚡ 1x</button>',
        '    <button id="btn-audio" title="Sound Toggle" style="background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);color:#cbd5e1;padding:4px 8px;border-radius:6px;cursor:pointer;font-size:12px;">🔊</button>',
        '    <button id="btn-market" style="background:linear-gradient(180deg,#16a34a,#15803d);border:1px solid #22c55e;color:#fff;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;display:flex;align-items:center;gap:4px;box-shadow:0 2px 8px rgba(34,197,94,0.3);">🏪 Рынок</button>',
        '    <button id="btn-orders" style="background:linear-gradient(180deg,#d97706,#b45309);border:1px solid #f59e0b;color:#fff;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;display:flex;align-items:center;gap:4px;box-shadow:0 2px 8px rgba(245,158,11,0.3);">📜 Заказы <span id="orders-badge" style="background:#ef4444;color:#fff;border-radius:10px;padding:0 5px;font-size:10px;margin-left:2px;">3</span></button>',
        '  </div>',
        '</div>'
    ].join('');
    container.appendChild(header);

    // 2. Main Game Viewport (Canvas + Side Panels)
    var mainView = document.createElement('div');
    mainView.style.cssText = 'position:relative;flex:1;display:flex;overflow:hidden;background:#152317;';

    // Canvas Element
    var canvas = document.createElement('canvas');
    canvas.style.cssText = 'width:100%;height:100%;display:block;cursor:crosshair;';
    mainView.appendChild(canvas);

    // Floating Tooltip
    var tooltip = document.createElement('div');
    tooltip.style.cssText = 'position:absolute;display:none;background:rgba(15,23,42,0.92);border:1px solid rgba(255,255,255,0.2);padding:6px 10px;border-radius:6px;font-size:12px;color:#f8fafc;pointer-events:none;z-index:30;box-shadow:0 4px 16px rgba(0,0,0,0.5);backdrop-filter:blur(6px);white-space:nowrap;';
    mainView.appendChild(tooltip);

    // Quick Stats / Info overlay on top of canvas
    var infoPill = document.createElement('div');
    infoPill.style.cssText = 'position:absolute;top:12px;left:12px;background:rgba(0,0,0,0.4);backdrop-filter:blur(6px);padding:6px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);font-size:12px;color:#94a3b8;display:flex;gap:12px;pointer-events:none;z-index:10;';
    infoPill.innerHTML = '<span>🌱 Грядки: <strong id="info-crops-count" style="color:#4ade80;">0</strong></span><span>🐔 Животные: <strong id="info-animals-count" style="color:#fcd34d;">3</strong></span><span>📦 Склад: <strong id="info-produce-count" style="color:#60a5fa;">0</strong></span>';
    mainView.appendChild(infoPill);

    container.appendChild(mainView);

    // 3. Bottom Toolbar & Seed Palette
    var toolbar = document.createElement('div');
    toolbar.style.cssText = 'height:72px;background:linear-gradient(180deg,rgba(25,35,28,0.95),rgba(15,22,17,0.98));border-top:1px solid rgba(255,255,255,0.12);display:flex;align-items:center;justify-content:space-between;padding:0 16px;gap:12px;z-index:20;backdrop-filter:blur(8px);';

    // Left tools
    var toolsLeft = document.createElement('div');
    toolsLeft.style.cssText = 'display:flex;align-items:center;gap:6px;';
    CONFIG.tools.forEach(function (t) {
        var btn = document.createElement('button');
        btn.id = 'tool-' + t.id;
        btn.className = 'farm-tool-btn';
        btn.dataset.tool = t.id;
        btn.title = t.nameRu + ' (' + t.desc + ')';
        btn.style.cssText = 'position:relative;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.14);color:#e2e8f0;padding:6px 10px;border-radius:8px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px;min-width:54px;transition:all 0.15s ease;';
        btn.innerHTML = '<span style="font-size:18px;">' + t.icon + '</span><span style="font-size:10px;font-weight:600;color:#cbd5e1;">' + t.nameRu + '</span>';
        if (t.cost) {
            btn.innerHTML += '<span style="position:absolute;top:-4px;right:-4px;background:#eab308;color:#000;font-size:9px;font-weight:700;padding:1px 4px;border-radius:6px;">' + t.cost + '🪙</span>';
        }
        toolsLeft.appendChild(btn);
    });
    toolbar.appendChild(toolsLeft);

    // Right Seed Selector
    var seedPicker = document.createElement('div');
    seedPicker.style.cssText = 'display:flex;align-items:center;gap:6px;background:rgba(0,0,0,0.3);padding:4px 8px;border-radius:10px;border:1px solid rgba(255,255,255,0.08);';
    seedPicker.innerHTML = '<span style="font-size:11px;font-weight:600;color:#94a3b8;margin-right:4px;">Семена:</span>';
    
    Object.keys(CONFIG.crops).forEach(function (cropKey) {
        var c = CONFIG.crops[cropKey];
        var seedBtn = document.createElement('button');
        seedBtn.id = 'seed-btn-' + (c.id || cropKey);
        seedBtn.className = 'farm-seed-btn';
        seedBtn.dataset.crop = (c.id || cropKey);
        var gSec = c.growth_sec || c.growthSec || 20;
        var sCost = c.seed_cost || c.seedCost || 10;
        var sPrice = c.sell_price || c.sellPrice || 25;
        seedBtn.title = (c.name_ru || c.nameRu) + ' (Рост: ' + gSec + 'с, Семя: ' + sCost + '🪙, Доход: ' + sPrice + '🪙)';
        seedBtn.style.cssText = 'position:relative;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);color:#e2e8f0;padding:4px 8px;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:4px;font-size:11px;font-weight:500;transition:all 0.15s ease;';
        seedBtn.innerHTML = '<span>' + c.icon + '</span><span>' + (c.name_ru || c.nameRu) + '</span><span id="seed-qty-' + (c.id || cropKey) + '" style="font-size:10px;color:#a3e635;font-weight:700;">x6</span>';
        seedPicker.appendChild(seedBtn);
    });
    toolbar.appendChild(seedPicker);

    container.appendChild(toolbar);

    // 4. Modal Container for Shop / Orders / Storage
    var modalOverlay = document.createElement('div');
    modalOverlay.id = 'farm-modal-overlay';
    modalOverlay.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.75);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;z-index:50;';
    
    var modalBox = document.createElement('div');
    modalBox.id = 'farm-modal-box';
    modalBox.style.cssText = 'width:640px;max-width:92%;max-height:85%;background:#1e2922;border:1px solid rgba(255,255,255,0.18);border-radius:14px;box-shadow:0 20px 50px rgba(0,0,0,0.7);display:flex;flex-direction:column;overflow:hidden;animation:popIn 0.2s ease-out;';
    modalOverlay.appendChild(modalBox);
    container.appendChild(modalOverlay);

    root.appendChild(container);

    // --- CANVAS RESIZE & DRAWING HELPERS ---
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;

    function resizeCanvas() {
        var rect = mainView.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        dpr = window.devicePixelRatio || 1;
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);
    }
    window.addEventListener('resize', resizeCanvas);
    setTimeout(resizeCanvas, 50);

    // --- UI UPDATERS ---
    function updateHUD() {
        var elCoins = document.getElementById('stat-coins');
        var elLvl = document.getElementById('stat-lvl');
        var elXpTxt = document.getElementById('stat-xp-txt');
        var elXpBar = document.getElementById('stat-xp-bar');
        var elClock = document.getElementById('farm-clock');
        var elCropsCount = document.getElementById('info-crops-count');
        var elAnimalsCount = document.getElementById('info-animals-count');
        var elProduceCount = document.getElementById('info-produce-count');

        if (elCoins) elCoins.innerText = State.coins;
        if (elLvl) elLvl.innerText = State.level;
        
        var nextXp = CONFIG.xpLevels[State.level] || 99999;
        var prevXp = CONFIG.xpLevels[State.level - 1] || 0;
        var currentLevelXp = Math.max(0, State.xp - prevXp);
        var neededLevelXp = Math.max(1, nextXp - prevXp);
        var pct = Math.min(100, Math.floor((currentLevelXp / neededLevelXp) * 100));

        if (elXpTxt) elXpTxt.innerText = State.xp + '/' + nextXp + ' XP';
        if (elXpBar) elXpBar.style.width = pct + '%';

        // Clock & Weather
        var hours = Math.floor(State.dayTime / 60) % 24;
        var mins = Math.floor(State.dayTime % 60);
        var timeStr = (hours < 10 ? '0' : '') + hours + ':' + (mins < 10 ? '0' : '') + mins + (hours >= 12 ? ' PM' : ' AM');
        var weatherIcon = State.weather === 'rain' ? '🌧️ Дождь' : (hours >= 20 || hours < 5 ? '🌙 Ночь' : (hours >= 17 ? '🌅 Закат' : '☀️ Солнечно'));
        if (elClock) elClock.innerText = 'День ' + State.dayCount + ' · ' + timeStr + ' · ' + weatherIcon;

        // Info pills
        var activeCrops = 0;
        for (var i = 0; i < State.grid.length; i++) {
            if (State.grid[i].crop) activeCrops++;
        }
        if (elCropsCount) elCropsCount.innerText = activeCrops;
        if (elAnimalsCount) elAnimalsCount.innerText = State.animals.length;

        var totalProduce = 0;
        for (var p in State.inventory.produce) {
            totalProduce += State.inventory.produce[p] || 0;
        }
        if (elProduceCount) elProduceCount.innerText = totalProduce;

        var pendingOrders = State.orders.filter(function (order) { return !order.completed; }).length;
        var ordersBadge = document.getElementById('orders-badge');
        if (ordersBadge) {
            ordersBadge.innerText = String(pendingOrders);
            ordersBadge.style.display = pendingOrders > 0 ? 'inline-block' : 'none';
        }

        // Update Seed counts in toolbar
        Object.keys(CONFIG.crops).forEach(function (cropKey) {
            var c = CONFIG.crops[cropKey];
            var cid = c.id || cropKey;
            var elQty = document.getElementById('seed-qty-' + cid);
            var btn = document.getElementById('seed-btn-' + cid);
            var count = State.inventory.seeds[cid] || 0;
            if (elQty) elQty.innerText = 'x' + count;
            if (btn) {
                var minLvl = c.min_level || c.minLevel || 1;
                if (minLvl > State.level) {
                    btn.style.opacity = '0.4';
                    btn.style.cursor = 'not-allowed';
                    btn.title = 'Откроется на ' + minLvl + ' уровне';
                } else {
                    btn.style.opacity = '1.0';
                    btn.style.cursor = 'pointer';
                }
            }
        });

        // Highlight active tool & seed
        var toolBtns = document.querySelectorAll('.farm-tool-btn');
        toolBtns.forEach(function (btn) {
            if (btn.dataset.tool === State.activeTool) {
                btn.style.background = 'linear-gradient(180deg,rgba(34,197,94,0.3),rgba(21,128,61,0.4))';
                btn.style.borderColor = '#4ade80';
                btn.style.boxShadow = '0 0 10px rgba(74,222,128,0.3)';
            } else {
                btn.style.background = 'rgba(255,255,255,0.06)';
                btn.style.borderColor = 'rgba(255,255,255,0.14)';
                btn.style.boxShadow = 'none';
            }
        });

        var seedBtns = document.querySelectorAll('.farm-seed-btn');
        seedBtns.forEach(function (btn) {
            if (btn.dataset.crop === State.selectedCrop && State.activeTool === 'seed') {
                btn.style.background = 'linear-gradient(180deg,rgba(234,179,8,0.3),rgba(161,98,7,0.4))';
                btn.style.borderColor = '#facc15';
            } else {
                btn.style.background = 'rgba(255,255,255,0.05)';
                btn.style.borderColor = 'rgba(255,255,255,0.1)';
            }
        });
    }

    // --- FLOATING TEXT & PARTICLES (using CSS Coordinates) ---
    function addFloatingText(x, y, text, color, size) {
        State.popups.push({
            x: x,
            y: y,
            text: text,
            color: color || '#facc15',
            size: size || 16,
            life: 1.0,
            vy: -1.4
        });
    }

    function addSparkles(x, y, color, count) {
        count = count || 10;
        for (var i = 0; i < count; i++) {
            State.particles.push({
                x: x,
                y: y,
                vx: (Math.random() - 0.5) * 4,
                vy: (Math.random() - 0.8) * 4,
                size: Math.random() * 4 + 2,
                color: color || '#fde047',
                life: 1.0,
                decay: Math.random() * 0.03 + 0.02
            });
        }
    }

    // --- DRAWING FUNCTIONS ---
    function getGridOrigin() {
        var w = (canvas.width / dpr) || 600;
        var h = (canvas.height / dpr) || 400;
        var gridPixelW = CONFIG.gridCols * CONFIG.tileSize;
        var gridPixelH = CONFIG.gridRows * CONFIG.tileSize;
        return {
            x: Math.floor((w - gridPixelW) / 2) + 20,
            y: Math.floor((h - gridPixelH) / 2) - 10
        };
    }

    function drawTile(tile, origin, mouseTile) {
        var x = origin.x + tile.col * CONFIG.tileSize;
        var y = origin.y + tile.row * CONFIG.tileSize;
        var s = CONFIG.tileSize;

        var isHover = mouseTile && mouseTile.col === tile.col && mouseTile.row === tile.row;

        // Ground base
        if (!tile.tilled) {
            // Lush Grass tile
            ctx.fillStyle = (tile.col + tile.row) % 2 === 0 ? '#437a34' : '#4b853a';
            ctx.fillRect(x, y, s, s);

            // Grass blades texture
            ctx.fillStyle = '#579e43';
            ctx.fillRect(x + 12, y + 14, 4, 6);
            ctx.fillRect(x + 38, y + 26, 4, 8);
            ctx.fillRect(x + 22, y + 42, 5, 5);

            // Tiny flower / clover
            if ((tile.col * 7 + tile.row * 13) % 5 === 0) {
                ctx.fillStyle = '#fef08a';
                ctx.beginPath();
                ctx.arc(x + 48, y + 48, 3, 0, Math.PI * 2);
                ctx.fill();
            }
        } else {
            // Tilled dirt
            ctx.fillStyle = tile.watered ? '#452b14' : '#714827';
            ctx.fillRect(x, y, s, s);

            // Soil Furrows
            ctx.fillStyle = tile.watered ? '#301c0c' : '#57361c';
            for (var furrow = 6; furrow < s; furrow += 14) {
                ctx.fillRect(x + 2, y + furrow, s - 4, 3);
            }

            // Water shine highlight if watered
            if (tile.watered) {
                ctx.fillStyle = 'rgba(96, 165, 250, 0.25)';
                ctx.fillRect(x + 4, y + 8, s - 8, 4);
                ctx.fillRect(x + 12, y + 22, s - 24, 3);
                ctx.fillRect(x + 8, y + 36, s - 16, 3);
            }

            // Fertilizer indicator
            if (tile.fertilized) {
                ctx.fillStyle = '#c084fc';
                for (var fz = 0; fz < 4; fz++) {
                    ctx.fillRect(x + 8 + fz * 14, y + 4, 3, 3);
                }
            }
        }

        // Tile border line
        ctx.strokeStyle = 'rgba(0,0,0,0.15)';
        ctx.lineWidth = 1;
        ctx.strokeRect(x, y, s, s);

        // Hover highlight
        if (isHover) {
            ctx.fillStyle = 'rgba(255, 255, 255, 0.22)';
            ctx.fillRect(x, y, s, s);
            ctx.strokeStyle = '#fde047';
            ctx.lineWidth = 2;
            ctx.strokeRect(x + 1, y + 1, s - 2, s - 2);
        }

        // Draw Sprinkler if placed
        if (tile.sprinkler) {
            ctx.fillStyle = '#94a3b8';
            ctx.fillRect(x + s / 2 - 4, y + s / 2 - 4, 8, 8);
            ctx.fillStyle = '#38bdf8';
            ctx.beginPath();
            ctx.arc(x + s / 2, y + s / 2, 3, 0, Math.PI * 2);
            ctx.fill();
        }

        // Draw Scarecrow if placed
        if (tile.scarecrow) {
            ctx.fillStyle = '#78350f';
            ctx.fillRect(x + s / 2 - 2, y + 10, 4, 44);
            ctx.fillRect(x + 12, y + 22, 40, 4);
            ctx.fillStyle = '#fef08a';
            ctx.beginPath();
            ctx.arc(x + s / 2, y + 14, 7, 0, Math.PI * 2);
            ctx.fill();
        }

        // Draw Crop
        if (tile.crop && tile.tilled) {
            drawCrop(tile, x, y, s);
        }
    }

    function drawCrop(tile, x, y, s) {
        var crop = CONFIG.crops[tile.crop];
        if (!crop) return;

        var cx = x + s / 2;
        var cy = y + s / 2 + 6;

        if (tile.stage === 0) {
            // Tiny seed holes / dots
            ctx.fillStyle = '#ca8a04';
            ctx.beginPath();
            ctx.arc(cx - 8, cy - 4, 2.5, 0, Math.PI * 2);
            ctx.arc(cx + 8, cy - 4, 2.5, 0, Math.PI * 2);
            ctx.arc(cx, cy + 4, 2.5, 0, Math.PI * 2);
            ctx.fill();
        } else if (tile.stage === 1) {
            // Sprout (2 small green leaves)
            ctx.fillStyle = '#84cc16';
            ctx.beginPath();
            ctx.ellipse(cx - 5, cy - 6, 4, 8, -Math.PI / 6, 0, Math.PI * 2);
            ctx.ellipse(cx + 5, cy - 6, 4, 8, Math.PI / 6, 0, Math.PI * 2);
            ctx.fill();
            ctx.fillStyle = '#4d7c0f';
            ctx.fillRect(cx - 1, cy - 2, 2, 8);
        } else if (tile.stage === 2) {
            // Growing bush / leafy foliage
            ctx.fillStyle = '#4ade80';
            ctx.beginPath();
            ctx.arc(cx - 8, cy - 8, 7, 0, Math.PI * 2);
            ctx.arc(cx + 8, cy - 8, 7, 0, Math.PI * 2);
            ctx.arc(cx, cy - 14, 9, 0, Math.PI * 2);
            ctx.fill();

            // Small budding fruit
            ctx.fillStyle = crop.color || '#facc15';
            ctx.beginPath();
            ctx.arc(cx - 4, cy - 6, 3.5, 0, Math.PI * 2);
            ctx.arc(cx + 5, cy - 8, 3.5, 0, Math.PI * 2);
            ctx.fill();
        } else if (tile.stage === 3) {
            // Fully Ripe & Ready to Harvest!
            var bob = Math.sin(Date.now() * 0.005 + tile.col * 2 + tile.row) * 2.5;

            // Golden / sparkling ready aura
            ctx.fillStyle = 'rgba(250, 204, 21, 0.2)';
            ctx.beginPath();
            ctx.arc(cx, cy - 10 + bob, 18, 0, Math.PI * 2);
            ctx.fill();

            ctx.font = '28px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(crop.icon || '🌾', cx, cy - 12 + bob);

            // Ready banner badge
            ctx.fillStyle = '#22c55e';
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.arc(cx + 16, cy - 24 + bob, 6, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
            ctx.fillStyle = '#ffffff';
            ctx.font = 'bold 9px sans-serif';
            ctx.fillText('✓', cx + 16, cy - 24 + bob);
        }

        // Growth progress ring if growing
        if (tile.stage < 3 && tile.growthProgress > 0) {
            ctx.strokeStyle = 'rgba(255,255,255,0.2)';
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            ctx.arc(x + s - 10, y + 10, 6, 0, Math.PI * 2);
            ctx.stroke();

            ctx.strokeStyle = '#4ade80';
            ctx.beginPath();
            ctx.arc(x + s - 10, y + 10, 6, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * tile.growthProgress);
            ctx.stroke();
        }
    }

    function drawAnimals() {
        State.animals.forEach(function (a) {
            var dx = a.targetX - a.x;
            var dy = a.targetY - a.y;
            var dist = Math.sqrt(dx * dx + dy * dy);
            if (dist > 1) {
                a.x += (dx / dist) * 0.5 * State.timeSpeed;
                a.y += (dy / dist) * 0.5 * State.timeSpeed;
            } else if (Math.random() < 0.01) {
                if (a.type === 'chicken') {
                    a.targetX = 40 + Math.random() * 100;
                    a.targetY = 280 + Math.random() * 80;
                } else {
                    a.targetX = 480 + Math.random() * 120;
                    a.targetY = 280 + Math.random() * 80;
                }
            }

            var bob = Math.sin(Date.now() * 0.006 + a.x) * 1.5;
            var aDef = CONFIG.animals[a.type];
            if (!aDef) return;

            // Shadow
            ctx.fillStyle = 'rgba(0,0,0,0.25)';
            ctx.beginPath();
            ctx.ellipse(a.x, a.y + 12, a.type === 'cow' ? 18 : 10, 6, 0, 0, Math.PI * 2);
            ctx.fill();

            // Animal emoji / sprite
            ctx.font = a.type === 'cow' ? '32px sans-serif' : '24px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(aDef.icon, a.x, a.y + bob);

            // Produce ready indicator bubble
            if (a.produceReady) {
                ctx.fillStyle = '#ffffff';
                ctx.strokeStyle = '#22c55e';
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                ctx.arc(a.x + 14, a.y - 14 + bob, 10, 0, Math.PI * 2);
                ctx.fill();
                ctx.stroke();
                ctx.font = '12px sans-serif';
                ctx.fillText(aDef.product_icon || aDef.productIcon || '🥚', a.x + 14, a.y - 14 + bob);
            } else if (!a.fed) {
                // Hungry icon
                ctx.fillStyle = 'rgba(239, 68, 68, 0.9)';
                ctx.beginPath();
                ctx.arc(a.x + 12, a.y - 12 + bob, 8, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = '#ffffff';
                ctx.font = 'bold 10px sans-serif';
                ctx.fillText('!', a.x + 12, a.y - 12 + bob);
            }
        });
    }

    function drawEnvironment(origin) {
        var w = (canvas.width / dpr) || 600;
        var h = (canvas.height / dpr) || 400;

        // Fences around main farm grid
        ctx.strokeStyle = '#854d0e';
        ctx.fillStyle = '#a16207';
        ctx.lineWidth = 3;

        // Left Barn & Chicken Coop
        ctx.fillStyle = '#991b1b';
        ctx.fillRect(origin.x - 140, origin.y + 40, 100, 80);
        ctx.fillStyle = '#dc2626';
        // Barn Roof
        ctx.beginPath();
        ctx.moveTo(origin.x - 150, origin.y + 40);
        ctx.lineTo(origin.x - 90, origin.y);
        ctx.lineTo(origin.x - 30, origin.y + 40);
        ctx.fill();
        ctx.fillStyle = '#fef08a';
        ctx.font = 'bold 12px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('BARN', origin.x - 90, origin.y + 32);
        ctx.fillStyle = '#451a03';
        ctx.fillRect(origin.x - 105, origin.y + 65, 30, 55);

        // Right Pasture & Water Pond
        ctx.fillStyle = '#38bdf8';
        ctx.beginPath();
        ctx.ellipse(origin.x + CONFIG.gridCols * CONFIG.tileSize + 80, origin.y + 80, 55, 35, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = 'rgba(255,255,255,0.4)';
        ctx.beginPath();
        ctx.ellipse(origin.x + CONFIG.gridCols * CONFIG.tileSize + 70, origin.y + 75, 25, 12, -0.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = '16px sans-serif';
        ctx.fillText('🦆', origin.x + CONFIG.gridCols * CONFIG.tileSize + 85, origin.y + 80);

        // Windmill in top right
        var wx = origin.x + CONFIG.gridCols * CONFIG.tileSize + 90;
        var wy = origin.y + 220;
        ctx.fillStyle = '#d97706';
        ctx.fillRect(wx - 15, wy - 40, 30, 60);
        var rot = Date.now() * 0.002;
        ctx.save();
        ctx.translate(wx, wy - 40);
        ctx.rotate(rot);
        ctx.fillStyle = '#fef08a';
        for (var b = 0; b < 4; b++) {
            ctx.rotate(Math.PI / 2);
            ctx.fillRect(0, -3, 36, 6);
        }
        ctx.restore();
    }

    function drawLightingAndWeather() {
        var w = (canvas.width / dpr) || 600;
        var h = (canvas.height / dpr) || 400;
        var hours = Math.floor(State.dayTime / 60) % 24;

        if (hours >= 20 || hours < 5) {
            // Night
            ctx.fillStyle = 'rgba(10, 15, 35, 0.65)';
            ctx.fillRect(0, 0, w, h);

            // Stars / Fireflies
            ctx.fillStyle = '#fef08a';
            for (var st = 0; st < 25; st++) {
                var sx = ((st * 97 + 13) % w);
                var sy = ((st * 61 + 37) % (h / 2));
                var alpha = 0.4 + Math.sin(Date.now() * 0.004 + st) * 0.4;
                ctx.fillStyle = 'rgba(254, 240, 138, ' + alpha + ')';
                ctx.fillRect(sx, sy, 2, 2);
            }
        } else if (hours >= 17 && hours < 20) {
            // Sunset
            var sunsetGrad = ctx.createLinearGradient(0, 0, 0, h);
            sunsetGrad.addColorStop(0, 'rgba(234, 88, 12, 0.25)');
            sunsetGrad.addColorStop(1, 'rgba(124, 45, 18, 0.1)');
            ctx.fillStyle = sunsetGrad;
            ctx.fillRect(0, 0, w, h);
        }

        // Rain weather
        if (State.weather === 'rain') {
            ctx.fillStyle = 'rgba(30, 58, 138, 0.18)';
            ctx.fillRect(0, 0, w, h);

            ctx.strokeStyle = 'rgba(147, 197, 253, 0.6)';
            ctx.lineWidth = 1.5;
            for (var r = 0; r < 45; r++) {
                var rx = (Math.sin(r * 43) * 1000 + Date.now() * 0.4) % w;
                var ry = (Math.cos(r * 29) * 1000 + Date.now() * 0.8) % h;
                if (rx < 0) rx += w;
                if (ry < 0) ry += h;
                ctx.beginPath();
                ctx.moveTo(rx, ry);
                ctx.lineTo(rx - 3, ry + 12);
                ctx.stroke();
            }
        }
    }

    function drawPopupsAndParticles() {
        for (var i = State.popups.length - 1; i >= 0; i--) {
            var p = State.popups[i];
            p.y += p.vy;
            p.life -= 0.02;
            if (p.life <= 0) {
                State.popups.splice(i, 1);
                continue;
            }
            ctx.save();
            ctx.globalAlpha = Math.max(0, p.life);
            ctx.fillStyle = p.color;
            ctx.font = 'bold ' + p.size + 'px sans-serif';
            ctx.textAlign = 'center';
            ctx.strokeStyle = '#000000';
            ctx.lineWidth = 3;
            ctx.strokeText(p.text, p.x, p.y);
            ctx.fillText(p.text, p.x, p.y);
            ctx.restore();
        }

        for (var j = State.particles.length - 1; j >= 0; j--) {
            var pt = State.particles[j];
            pt.x += pt.vx;
            pt.y += pt.vy;
            pt.life -= pt.decay || 0.02;
            if (pt.rotation !== undefined) pt.rotation += pt.vRot || 0;
            if (pt.life <= 0) {
                State.particles.splice(j, 1);
                continue;
            }
            ctx.save();
            ctx.globalAlpha = Math.max(0, pt.life);
            ctx.fillStyle = pt.color;
            ctx.translate(pt.x, pt.y);
            if (pt.rotation) ctx.rotate((pt.rotation * Math.PI) / 180);
            ctx.fillRect(-pt.size / 2, -pt.size / 2, pt.size, pt.size);
            ctx.restore();
        }
    }

    // --- MOUSE & TOUCH INTERACTION ---
    var mousePos = { x: -1, y: -1 };
    var isMouseDown = false;

    function getTileAtPoint(px, py) {
        var origin = getGridOrigin();
        var col = Math.floor((px - origin.x) / CONFIG.tileSize);
        var row = Math.floor((py - origin.y) / CONFIG.tileSize);
        if (col >= 0 && col < CONFIG.gridCols && row >= 0 && row < CONFIG.gridRows) {
            for (var i = 0; i < State.grid.length; i++) {
                if (State.grid[i].col === col && State.grid[i].row === row) {
                    return State.grid[i];
                }
            }
        }
        return null;
    }

    function getAnimalAtPoint(px, py) {
        for (var i = 0; i < State.animals.length; i++) {
            var a = State.animals[i];
            var dist = Math.hypot(px - a.x, py - a.y);
            if (dist < 26) return a;
        }
        return null;
    }

    function performActionOnTile(tile) {
        if (!tile) return;
        var origin = getGridOrigin();
        var tx = origin.x + tile.col * CONFIG.tileSize + CONFIG.tileSize / 2;
        var ty = origin.y + tile.row * CONFIG.tileSize + CONFIG.tileSize / 2;

        if (State.activeTool === 'hoe') {
            if (!tile.tilled) {
                tile.tilled = true;
                tile.watered = false;
                tile.crop = null;
                tile.stage = 0;
                tile.growthProgress = 0;
                AudioSys.playTill();
                addSparkles(tx, ty, '#a16207', 8);
                State.addXP(1);
            }
        } else if (State.activeTool === 'water') {
            if (tile.tilled && !tile.watered) {
                tile.watered = true;
                AudioSys.playWater();
                addSparkles(tx, ty, '#60a5fa', 10);
                addFloatingText(tx, ty - 10, '💧 Watered', '#60a5fa', 13);
                State.addXP(1);
            }
        } else if (State.activeTool === 'seed') {
            if (tile.tilled && !tile.crop) {
                var cropId = State.selectedCrop;
                var cropDef = CONFIG.crops[cropId];
                var minLvl = cropDef ? (cropDef.min_level || cropDef.minLevel || 1) : 1;
                var sCost = cropDef ? (cropDef.seed_cost || cropDef.seedCost || 10) : 10;
                var cName = cropDef ? (cropDef.name_ru || cropDef.nameRu || cropId) : cropId;

                if (cropDef && minLvl <= State.level) {
                    var available = State.inventory.seeds[cropId] || 0;
                    if (available > 0) {
                        State.inventory.seeds[cropId]--;
                        tile.crop = cropId;
                        tile.stage = 0;
                        tile.growthProgress = 0;
                        AudioSys.playPlant();
                        addSparkles(tx, ty, '#4ade80', 8);
                        addFloatingText(tx, ty - 10, '🌱 ' + cName, '#4ade80', 13);
                        State.addXP(2);
                    } else if (State.coins >= sCost) {
                        State.coins -= sCost;
                        tile.crop = cropId;
                        tile.stage = 0;
                        tile.growthProgress = 0;
                        AudioSys.playPlant();
                        addSparkles(tx, ty, '#4ade80', 8);
                        addFloatingText(tx, ty - 10, '🌱 ' + cName + ' (-' + sCost + '🪙)', '#facc15', 13);
                        State.addXP(2);
                    } else {
                        addFloatingText(tx, ty - 10, 'Нет семян и монет!', '#f87171', 13);
                    }
                }
            }
        } else if (State.activeTool === 'scythe') {
            // Mass harvesting 3x3 surrounding ready crops
            var harvestedAny = false;
            for (var dr = -1; dr <= 1; dr++) {
                for (var dc = -1; dc <= 1; dc++) {
                    var nCol = tile.col + dc;
                    var nRow = tile.row + dr;
                    var neighborTile = State.grid.find(function (t) { return t.col === nCol && t.row === nRow; });
                    if (neighborTile && neighborTile.crop && neighborTile.stage === 3) {
                        harvestSingleTile(neighborTile, origin);
                        harvestedAny = true;
                    }
                }
            }
            if (!harvestedAny && tile.crop && tile.stage === 3) {
                harvestSingleTile(tile, origin);
            }
        } else if (State.activeTool === 'harvest' || State.activeTool === 'hand') {
            if (tile.crop && tile.stage === 3) {
                harvestSingleTile(tile, origin);
            } else if (State.activeTool === 'hand' && !tile.tilled) {
                addFloatingText(tx, ty - 10, 'Вспаши тяпкой ⛏️', '#cbd5e1', 12);
            }
        } else if (State.activeTool === 'fertilizer') {
            if (tile.crop && tile.stage < 3 && !tile.fertilized) {
                if (State.inventory.fertilizer > 0 || State.coins >= 15) {
                    if (State.inventory.fertilizer > 0) {
                        State.inventory.fertilizer--;
                    } else {
                        State.coins -= 15;
                    }
                    tile.fertilized = true;
                    tile.growthProgress = Math.min(1.0, tile.growthProgress + 0.5);
                    if (tile.growthProgress >= 1.0) tile.stage = 3;
                    else if (tile.growthProgress >= 0.5) tile.stage = 2;
                    AudioSys.playHarvest();
                    addSparkles(tx, ty, '#c084fc', 14);
                    addFloatingText(tx, ty - 10, '✨ Удобрено! (+50%)', '#c084fc', 14);
                }
            }
        }
        updateHUD();
        State.saveToBackend();
    }

    function harvestSingleTile(t, origin) {
        var cropInfo = CONFIG.crops[t.crop];
        if (!cropInfo) return;
        var sPrice = cropInfo.sell_price || cropInfo.sellPrice || 12;
        var bonusMult = (t.scarecrow || (State.upgrades.scarecrows && State.upgrades.scarecrows.length > 0)) ? 1.2 : 1.0;
        var earnCoins = Math.floor(sPrice * bonusMult);

        State.inventory.produce[t.crop] = (State.inventory.produce[t.crop] || 0) + 1;
        State.coins += earnCoins;
        State.stats.cropsHarvested++;
        State.stats.coinsEarned += earnCoins;
        State.addXP(cropInfo.xp || 5);

        AudioSys.playHarvest();
        AudioSys.playCoin();
        var hx = origin.x + t.col * CONFIG.tileSize + CONFIG.tileSize / 2;
        var hy = origin.y + t.row * CONFIG.tileSize + CONFIG.tileSize / 2;
        addSparkles(hx, hy, '#facc15', 16);
        addFloatingText(hx, hy - 16, '+' + earnCoins + ' 🪙 ' + (cropInfo.icon || '🌾'), '#facc15', 18);

        // Reset tile
        t.crop = null;
        t.stage = 0;
        t.growthProgress = 0;
        t.watered = false;
        t.fertilized = false;
    }

    function performActionOnAnimal(animal) {
        if (!animal) return;
        var aDef = CONFIG.animals[animal.type];
        if (!aDef) return;

        var prodSell = aDef.product_sell || aDef.productSell || 45;
        var pName = aDef.product_name_ru || aDef.productNameRu || aDef.product;
        var pIcon = aDef.product_icon || aDef.productIcon || '🥚';

        if (animal.produceReady) {
            animal.produceReady = false;
            animal.progress = 0;
            animal.fed = false;

            State.inventory.produce[aDef.product] = (State.inventory.produce[aDef.product] || 0) + 1;
            State.coins += prodSell;
            State.stats.coinsEarned += prodSell;
            State.addXP(aDef.xp || 20);

            AudioSys.playAnimal(animal.type);
            AudioSys.playCoin();
            addSparkles(animal.x, animal.y, '#facc15', 16);
            addFloatingText(animal.x, animal.y - 20, '+' + prodSell + ' 🪙 ' + pIcon, '#facc15', 18);
        } else if (State.activeTool === 'feed' || !animal.fed) {
            if (State.inventory.feed > 0 || State.coins >= 8) {
                if (State.inventory.feed > 0) {
                    State.inventory.feed--;
                } else {
                    State.coins -= 8;
                }
                animal.fed = true;
                AudioSys.playAnimal(animal.type);
                addSparkles(animal.x, animal.y, '#ec4899', 12);
                addFloatingText(animal.x, animal.y - 18, '❤️ Накормлен!', '#ec4899', 14);
                State.addXP(5);
            } else {
                addFloatingText(animal.x, animal.y - 18, 'Нужен корм (8🪙)', '#f87171', 12);
            }
        } else {
            AudioSys.playAnimal(animal.type);
            addFloatingText(animal.x, animal.y - 18, '❤️ ' + (aDef.name_ru || aDef.nameRu || aDef.name), '#f472b6', 14);
        }
        updateHUD();
        State.saveToBackend();
    }

    // Canvas Event Listeners
    canvas.addEventListener('mousemove', function (e) {
        var rect = canvas.getBoundingClientRect();
        mousePos.x = e.clientX - rect.left;
        mousePos.y = e.clientY - rect.top;

        var tile = getTileAtPoint(mousePos.x, mousePos.y);
        var animal = getAnimalAtPoint(mousePos.x, mousePos.y);

        if (animal) {
            var aDef = CONFIG.animals[animal.type];
            tooltip.style.display = 'block';
            tooltip.style.left = (mousePos.x + 16) + 'px';
            tooltip.style.top = (mousePos.y + 16) + 'px';
            var aName = aDef ? (aDef.name_ru || aDef.nameRu || aDef.name) : animal.type;
            var aIcon = aDef ? aDef.icon : '🐔';
            var pIcon = aDef ? (aDef.product_icon || aDef.productIcon) : '🥚';
            var pName = aDef ? (aDef.product_name_ru || aDef.productNameRu) : 'Продукт';

            tooltip.innerHTML = '<strong>' + aIcon + ' ' + aName + '</strong><br>' +
                (animal.produceReady ? '<span style="color:#4ade80;">✓ Готов продукт: ' + pIcon + ' ' + pName + '</span>' :
                (animal.fed ? '<span style="color:#60a5fa;">⏳ Производство: ' + Math.floor(animal.progress * 100) + '%</span>' : '<span style="color:#f87171;">⚠️ Голоден! Накорми</span>'));
        } else if (tile) {
            tooltip.style.display = 'block';
            tooltip.style.left = (mousePos.x + 16) + 'px';
            tooltip.style.top = (mousePos.y + 16) + 'px';
            if (tile.crop) {
                var cDef = CONFIG.crops[tile.crop];
                var cName = cDef ? (cDef.name_ru || cDef.nameRu || tile.crop) : tile.crop;
                var cPrice = cDef ? (cDef.sell_price || cDef.sellPrice) : 12;
                tooltip.innerHTML = '<strong>' + (cDef ? cDef.icon : '🌱') + ' ' + cName + '</strong><br>' +
                    (tile.stage === 3 ? '<span style="color:#4ade80;font-weight:700;">✨ Созрело! Собери (+' + cPrice + '🪙)</span>' :
                    '<span style="color:#facc15;">Рост: ' + Math.floor(tile.growthProgress * 100) + '% ' + (tile.watered ? '💧' : '☀️') + '</span>');
            } else if (tile.tilled) {
                tooltip.innerHTML = '<strong>Грядка</strong><br>' + (tile.watered ? '<span style="color:#60a5fa;">💧 Полит</span>' : 'Сухая земля. Полей или посади!');
            } else {
                tooltip.innerHTML = '<strong>Трава</strong><br>Вспаши мотыгой ⛏️';
            }
        } else {
            tooltip.style.display = 'none';
        }

        if (isMouseDown && tile) {
            performActionOnTile(tile);
        }
    });

    canvas.addEventListener('mousedown', function (e) {
        isMouseDown = true;
        AudioSys.init();
        var rect = canvas.getBoundingClientRect();
        var px = e.clientX - rect.left;
        var py = e.clientY - rect.top;

        var animal = getAnimalAtPoint(px, py);
        if (animal) {
            performActionOnAnimal(animal);
            return;
        }

        var tile = getTileAtPoint(px, py);
        if (tile) {
            performActionOnTile(tile);
        }
    });

    window.addEventListener('mouseup', function () {
        isMouseDown = false;
    });

    // Touch Support
    canvas.addEventListener('touchstart', function (e) {
        if (e.touches.length > 0) {
            AudioSys.init();
            var rect = canvas.getBoundingClientRect();
            var px = e.touches[0].clientX - rect.left;
            var py = e.touches[0].clientY - rect.top;
            var animal = getAnimalAtPoint(px, py);
            if (animal) {
                performActionOnAnimal(animal);
                return;
            }
            var tile = getTileAtPoint(px, py);
            if (tile) performActionOnTile(tile);
        }
    }, { passive: true });

    // --- BUTTON HANDLERS ---
    document.querySelectorAll('.farm-tool-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            AudioSys.playClick();
            State.activeTool = this.dataset.tool;
            updateHUD();
        });
    });

    document.querySelectorAll('.farm-seed-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            AudioSys.playClick();
            State.selectedCrop = this.dataset.crop;
            State.activeTool = 'seed';
            updateHUD();
        });
    });

    document.getElementById('btn-speed').addEventListener('click', function () {
        AudioSys.playClick();
        if (State.timeSpeed === 1) State.timeSpeed = 2;
        else if (State.timeSpeed === 2) State.timeSpeed = 5;
        else State.timeSpeed = 1;
        this.innerText = '⚡ ' + State.timeSpeed + 'x';
    });

    document.getElementById('btn-audio').addEventListener('click', function () {
        AudioSys.muted = !AudioSys.muted;
        this.innerText = AudioSys.muted ? '🔇' : '🔊';
    });

    // --- MODALS (Market & Orders) ---
    function openModal(title, contentHtml) {
        modalBox.innerHTML = [
            '<div style="display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid rgba(255,255,255,0.12);background:rgba(0,0,0,0.2);">',
            '  <h3 style="margin:0;font-size:18px;color:#facc15;display:flex;align-items:center;gap:8px;">' + title + '</h3>',
            '  <button id="modal-close-btn" style="background:none;border:none;color:#94a3b8;font-size:22px;cursor:pointer;line-height:1;">&times;</button>',
            '</div>',
            '<div style="padding:16px 20px;overflow-y:auto;max-height:500px;">' + contentHtml + '</div>'
        ].join('');
        modalOverlay.style.display = 'flex';

        document.getElementById('modal-close-btn').addEventListener('click', function () {
            AudioSys.playClick();
            modalOverlay.style.display = 'none';
        });
    }

    modalOverlay.addEventListener('click', function (e) {
        if (e.target === modalOverlay) {
            modalOverlay.style.display = 'none';
        }
    });

    // Market Modal Handler
    document.getElementById('btn-market').addEventListener('click', function () {
        AudioSys.playClick();
        var html = '<div style="display:flex;flex-direction:column;gap:18px;">';

        // 1. Buy Seeds Section
        html += '<div><h4 style="margin:0 0 10px 0;color:#86efac;font-size:14px;">🌱 Купить семена (Магазин семян)</h4><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;">';
        Object.keys(CONFIG.crops).forEach(function (cropKey) {
            var c = CONFIG.crops[cropKey];
            var cid = c.id || cropKey;
            var minLvl = c.min_level || c.minLevel || 1;
            var sCost = c.seed_cost || c.seedCost || 10;
            var sPrice = c.sell_price || c.sellPrice || 25;
            var gSec = c.growth_sec || c.growthSec || 20;
            var cName = c.name_ru || c.nameRu || cid;
            var unlocked = minLvl <= State.level;

            html += '<div style="background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:10px;display:flex;flex-direction:column;justify-content:space-between;gap:8px;' + (!unlocked ? 'opacity:0.5;' : '') + '">' +
                '<div style="display:flex;align-items:center;gap:8px;">' +
                '<span style="font-size:24px;">' + (c.icon || '🌱') + '</span>' +
                '<div><div style="font-weight:600;font-size:13px;color:#f8fafc;">' + cName + '</div><div style="font-size:11px;color:#94a3b8;">Рост: ' + gSec + 'с · ' + sPrice + '🪙</div></div>' +
                '</div>' +
                (unlocked ?
                '<div style="display:flex;gap:4px;">' +
                '<button class="buy-seed-btn" data-crop="' + cid + '" data-qty="1" style="flex:1;background:#16a34a;border:none;color:#fff;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600;">1шт (' + sCost + '🪙)</button>' +
                '<button class="buy-seed-btn" data-crop="' + cid + '" data-qty="5" style="flex:1;background:#15803d;border:none;color:#fff;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600;">5шт (' + (sCost * 5) + '🪙)</button>' +
                '</div>' : '<div style="font-size:11px;color:#f87171;">🔒 Требуется ' + minLvl + ' уровень</div>') +
                '</div>';
        });
        html += '</div></div>';

        // 2. Buy Livestock Section
        html += '<div><h4 style="margin:0 0 10px 0;color:#fcd34d;font-size:14px;">🐔 Домашние животные (Фермерский загон)</h4><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;">';
        Object.keys(CONFIG.animals).forEach(function (animKey) {
            var a = CONFIG.animals[animKey];
            var aid = a.id || animKey;
            var minLvl = a.min_level || a.minLevel || 1;
            var aCost = a.cost || 100;
            var aName = a.name_ru || a.nameRu || aid;
            var pName = a.product_name_ru || a.productNameRu || a.product;
            var pIcon = a.product_icon || a.productIcon || '🥚';
            var pSell = a.product_sell || a.productSell || 40;
            var unlocked = minLvl <= State.level;

            html += '<div style="background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:10px;display:flex;flex-direction:column;justify-content:space-between;gap:8px;' + (!unlocked ? 'opacity:0.5;' : '') + '">' +
                '<div style="display:flex;align-items:center;gap:8px;">' +
                '<span style="font-size:24px;">' + a.icon + '</span>' +
                '<div><div style="font-weight:600;font-size:13px;color:#f8fafc;">' + aName + '</div><div style="font-size:11px;color:#94a3b8;">Дает: ' + pIcon + ' ' + pName + ' (' + pSell + '🪙)</div></div>' +
                '</div>' +
                (unlocked ?
                '<button class="buy-animal-btn" data-animal="' + aid + '" style="background:#d97706;border:none;color:#fff;padding:6px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;">Купить за ' + aCost + '🪙</button>' :
                '<div style="font-size:11px;color:#f87171;">🔒 Требуется ' + minLvl + ' уровень</div>') +
                '</div>';
        });
        html += '</div></div>';

        // 3. Farm Upgrades Section
        html += '<div><h4 style="margin:0 0 10px 0;color:#c4b5fd;font-size:14px;">🏗️ Улучшения фермы</h4><div style="display:grid;grid-template-columns:repeat(3,minmax(150px,1fr));gap:10px;">';
        Object.keys(CONFIG.upgrades || {}).forEach(function (upgradeKey) {
            var u = CONFIG.upgrades[upgradeKey];
            var uid = u.id || upgradeKey;
            var minLvl = u.min_level || u.minLevel || 1;
            var owned = uid === 'silo' ? (State.upgrades.silo || 0) : (State.upgrades[uid + 's'] || []).length;
            var uName = u.name_ru || u.nameRu || uid;
            var unlocked = minLvl <= State.level;
            html += '<div style="background:rgba(76,29,149,0.18);border:1px solid rgba(167,139,250,0.25);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px;">' +
                '<div style="font-size:22px;">' + (u.icon || '🏗️') + '</div><div style="font-weight:650;font-size:12px;">' + uName + (owned ? ' · ' + owned : '') + '</div>' +
                '<div style="font-size:10px;color:#94a3b8;min-height:30px;">' + (u.desc || '') + '</div>' +
                (unlocked ? '<button class="buy-upgrade-btn" data-upgrade="' + uid + '" style="background:#7c3aed;border:none;color:#fff;padding:5px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:650;">Купить за ' + u.cost + '🪙</button>' : '<span style="font-size:10px;color:#f87171;">🔒 Уровень ' + minLvl + '</span>') + '</div>';
        });
        html += '</div></div>';

        // 4. Sell Storage Produce
        html += '<div><h4 style="margin:0 0 10px 0;color:#60a5fa;font-size:14px;">📦 Склад урожая и продуктов (Продажа)</h4><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;">';
        var hasProduce = false;
        for (var prodKey in State.inventory.produce) {
            var count = State.inventory.produce[prodKey] || 0;
            if (count > 0) {
                hasProduce = true;
                var pDef = CONFIG.crops[prodKey] || (CONFIG.animals[prodKey === 'egg' ? 'chicken' : (prodKey === 'milk' ? 'cow' : 'sheep')]);
                var pName = pDef ? (pDef.name_ru || pDef.nameRu || pDef.product_name_ru || pDef.productNameRu || prodKey) : prodKey;
                var pIcon = pDef ? (pDef.icon || pDef.product_icon || pDef.productIcon || '📦') : '📦';
                var pPrice = pDef ? (pDef.sell_price || pDef.sellPrice || pDef.product_sell || pDef.productSell || 10) : 10;
                html += '<div style="background:rgba(0,0,0,0.25);border:1px solid rgba(255,255,255,0.06);border-radius:6px;padding:8px;text-align:center;">' +
                    '<div style="font-size:20px;">' + pIcon + '</div>' +
                    '<div style="font-size:12px;font-weight:600;">' + pName + ' x' + count + '</div>' +
                    '<div style="font-size:11px;color:#facc15;margin-bottom:6px;">+' + (count * pPrice) + '🪙</div>' +
                    '<button class="sell-produce-btn" data-produce="' + prodKey + '" style="background:#2563eb;border:none;color:#fff;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px;width:100%;">Продать всё</button>' +
                    '</div>';
            }
        }
        if (!hasProduce) {
            html += '<div style="color:#94a3b8;font-size:12px;grid-column:1/-1;">Склад пуст. Вырастите урожай или соберите продукты с животных!</div>';
        }
        html += '</div></div></div>';

        openModal('🏪 Фермерский Рынок (Marketplace)', html);

        var viewW = (canvas.width / dpr) || 600;
        var viewH = (canvas.height / dpr) || 400;

        document.querySelectorAll('.buy-seed-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var cropId = this.dataset.crop;
                var qty = parseInt(this.dataset.qty, 10);
                var c = CONFIG.crops[cropId];
                var sCost = c.seed_cost || c.seedCost || 10;
                var cName = c.name_ru || c.nameRu || cropId;
                var totalCost = sCost * qty;
                if (State.coins >= totalCost) {
                    State.coins -= totalCost;
                    State.inventory.seeds[cropId] = (State.inventory.seeds[cropId] || 0) + qty;
                    AudioSys.playCoin();
                    addFloatingText(viewW / 2, viewH / 2, '+' + qty + ' ' + cName + ' 🌱', '#4ade80', 18);
                    updateHUD();
                    State.saveToBackend();
                    document.getElementById('btn-market').click();
                } else {
                    AudioSys.playClick();
                    addFloatingText(viewW / 2, viewH / 2, 'Недостаточно монет: нужно ' + totalCost + '🪙', '#f87171', 18);
                }
            });
        });

        document.querySelectorAll('.buy-animal-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var animId = this.dataset.animal;
                var a = CONFIG.animals[animId];
                var aCost = a.cost || 100;
                var aName = a.name_ru || a.nameRu || animId;
                if (State.coins >= aCost) {
                    State.coins -= aCost;
                    var newAnim = {
                        id: animId + '_' + Date.now(),
                        type: animId,
                        x: animId === 'chicken' ? 50 + Math.random() * 80 : 520 + Math.random() * 100,
                        y: 300 + Math.random() * 60,
                        targetX: animId === 'chicken' ? 60 : 540,
                        targetY: 320,
                        fed: true,
                        progress: 0,
                        produceReady: false
                    };
                    State.animals.push(newAnim);
                    AudioSys.playAnimal(animId);
                    AudioSys.playCoin();
                    addFloatingText(viewW / 2, viewH / 2, '+1 ' + aName + ' ' + a.icon, '#facc15', 18);
                    updateHUD();
                    State.saveToBackend();
                    document.getElementById('btn-market').click();
                } else {
                    AudioSys.playClick();
                    addFloatingText(viewW / 2, viewH / 2, 'Недостаточно монет: нужно ' + aCost + '🪙', '#f87171', 18);
                }
            });
        });

        document.querySelectorAll('.buy-upgrade-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var uid = this.dataset.upgrade;
                var u = CONFIG.upgrades[uid];
                if (!u || State.coins < u.cost) {
                    addFloatingText(viewW / 2, viewH / 2, 'Недостаточно монет для улучшения', '#f87171', 18);
                    return;
                }
                var targetTile = null;
                if (uid === 'sprinkler' || uid === 'scarecrow') {
                    targetTile = State.grid.find(function (t) { return t.tilled && !t.crop && !t.sprinkler && !t.scarecrow; });
                    if (!targetTile) {
                        addFloatingText(viewW / 2, viewH / 2, 'Нет свободной грядки для улучшения', '#f87171', 18);
                        return;
                    }
                }
                State.coins -= u.cost;
                if (uid === 'sprinkler') {
                    targetTile.sprinkler = true;
                    State.upgrades.sprinklers.push({ col: targetTile.col, row: targetTile.row });
                } else if (uid === 'scarecrow') {
                    targetTile.scarecrow = true;
                    State.upgrades.scarecrows.push({ col: targetTile.col, row: targetTile.row });
                } else if (uid === 'silo') {
                    State.upgrades.silo = (State.upgrades.silo || 0) + 1;
                    State.inventory.feed += 10;
                    State.inventory.fertilizer += 5;
                }
                AudioSys.playLevelUp();
                addFloatingText(viewW / 2, viewH / 2, 'Улучшение куплено: ' + (u.icon || '') + ' ' + (u.name_ru || u.nameRu), '#c4b5fd', 19);
                updateHUD();
                State.saveToBackend();
                document.getElementById('btn-market').click();
            });
        });

        document.querySelectorAll('.sell-produce-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var pKey = this.dataset.produce;
                var count = State.inventory.produce[pKey] || 0;
                var pDef = CONFIG.crops[pKey] || (CONFIG.animals[pKey === 'egg' ? 'chicken' : (pKey === 'milk' ? 'cow' : 'sheep')]);
                var pPrice = pDef ? (pDef.sell_price || pDef.sellPrice || pDef.product_sell || pDef.productSell || 10) : 10;
                var earned = count * pPrice;
                if (earned > 0) {
                    State.coins += earned;
                    State.inventory.produce[pKey] = 0;
                    State.stats.coinsEarned += earned;
                    AudioSys.playCoin();
                    addFloatingText(viewW / 2, viewH / 2, '+' + earned + ' 🪙', '#facc15', 20);
                    updateHUD();
                    State.saveToBackend();
                    document.getElementById('btn-market').click();
                }
            });
        });
    });

    // Orders Modal Handler
    document.getElementById('btn-orders').addEventListener('click', function () {
        AudioSys.playClick();
        var html = '<div style="display:flex;flex-direction:column;gap:12px;">';
        State.orders.forEach(function (order) {
            var canComplete = true;
            var reqList = [];
            for (var item in order.req) {
                var needed = order.req[item];
                var have = State.inventory.produce[item] || 0;
                if (have < needed) canComplete = false;
                var def = CONFIG.crops[item] || (CONFIG.animals[item === 'egg' ? 'chicken' : (item === 'milk' ? 'cow' : 'sheep')]);
                var iIcon = def ? (def.icon || def.product_icon || def.productIcon || '📦') : '📦';
                var iName = def ? (def.name_ru || def.nameRu || def.product_name_ru || def.productNameRu || item) : item;
                reqList.push('<span style="color:' + (have >= needed ? '#4ade80' : '#f87171') + ';font-weight:600;">' + iIcon + ' ' + iName + ': ' + have + '/' + needed + '</span>');
            }

            html += '<div style="background:rgba(0,0,0,0.3);border:1px solid ' + (order.completed ? 'rgba(74,222,128,0.3)' : 'rgba(255,255,255,0.1)') + ';border-radius:10px;padding:12px;display:flex;justify-content:space-between;align-items:center;gap:14px;">' +
                '<div style="display:flex;align-items:center;gap:12px;">' +
                '<div style="font-size:32px;line-height:1;background:rgba(255,255,255,0.06);padding:6px;border-radius:8px;">' + order.avatar + '</div>' +
                '<div>' +
                '<div style="font-weight:700;font-size:14px;color:#f8fafc;">' + order.villager + '</div>' +
                '<div style="font-size:12px;color:#cbd5e1;margin:2px 0 6px 0;">«' + order.text + '»</div>' +
                '<div style="font-size:11px;display:flex;gap:10px;">' + reqList.join(' · ') + '</div>' +
                '</div>' +
                '</div>' +
                '<div style="text-align:right;min-width:110px;">' +
                '<div style="font-size:13px;font-weight:700;color:#facc15;">+' + order.rewardCoins + ' 🪙</div>' +
                '<div style="font-size:11px;color:#60a5fa;margin-bottom:6px;">+' + order.rewardXp + ' XP</div>' +
                (!order.completed ?
                '<button class="complete-order-btn" data-order-id="' + order.id + '" style="background:' + (canComplete ? '#16a34a' : 'rgba(255,255,255,0.1)') + ';border:none;color:' + (canComplete ? '#fff' : '#64748b') + ';padding:5px 10px;border-radius:6px;cursor:' + (canComplete ? 'pointer' : 'not-allowed') + ';font-size:11px;font-weight:600;">' + (canComplete ? 'Выполнить!' : 'Не готово') + '</button>' :
                '<span style="color:#4ade80;font-size:12px;font-weight:700;">✓ Выполнен!</span>') +
                '</div>' +
                '</div>';
        });
        html += '</div>';

        openModal('📜 Доска заказов жителей деревни (Village Orders)', html);

        var viewW = (canvas.width / dpr) || 600;
        var viewH = (canvas.height / dpr) || 400;

        document.querySelectorAll('.complete-order-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var oId = parseInt(this.dataset.orderId, 10);
                var ord = State.orders.find(function (o) { return o.id === oId; });
                if (!ord || ord.completed) return;

                var canDo = true;
                for (var item in ord.req) {
                    if ((State.inventory.produce[item] || 0) < ord.req[item]) canDo = false;
                }

                if (canDo) {
                    for (var r in ord.req) {
                        State.inventory.produce[r] -= ord.req[r];
                    }
                    State.coins += ord.rewardCoins;
                    State.stats.coinsEarned += ord.rewardCoins;
                    State.stats.ordersCompleted++;
                    State.addXP(ord.rewardXp);
                    ord.completed = true;

                    AudioSys.playLevelUp();
                    addFloatingText(viewW / 2, viewH / 2, 'ЗАКАЗ ВЫПОЛНЕН! +' + ord.rewardCoins + '🪙 +' + ord.rewardXp + 'XP 🎉', '#facc15', 22);

                    setTimeout(function () {
                        ord.completed = false;
                        ord.rewardCoins += 50;
                        ord.rewardXp += 25;
                    }, 15000);

                    updateHUD();
                    State.saveToBackend();
                    document.getElementById('btn-orders').click();
                }
            });
        });
    });

    // --- MAIN GAME LOOP (60 FPS) WITH EXPLICIT LIFECYCLE ---
    var lastTick = Date.now();
    var lastSecond = Date.now();
    var animId = null;
    var isRunning = true;

    function gameLoop() {
        if (!isRunning) return;

        var now = Date.now();
        var dt = (now - lastTick) / 1000;
        lastTick = now;

        if (dt > 0.5) dt = 0.5;
        dt *= State.timeSpeed;

        // 1. Advance Day Time (1 real sec = 2 game mins at 1x speed)
        State.dayTime += dt * 2.0;
        if (State.dayTime >= 1440) {
            State.dayTime -= 1440;
            State.dayCount++;

            if (Math.random() < 0.3) {
                State.weather = 'rain';
            } else {
                State.weather = 'sunny';
            }
        }

        // If rain, auto-water all tilled plots!
        if (State.weather === 'rain') {
            State.grid.forEach(function (tile) {
                if (tile.tilled && !tile.watered) {
                    tile.watered = true;
                }
            });
        }

        // Auto-sprinklers keep all tilled soil watered once purchased.
        if (State.upgrades.sprinklers && State.upgrades.sprinklers.length > 0) {
            State.grid.forEach(function (tile) {
                if (tile.tilled) tile.watered = true;
            });
        }

        // 2. Advance Crops Growth
        State.grid.forEach(function (tile) {
            if (tile.crop && tile.stage < 3) {
                var cropDef = CONFIG.crops[tile.crop];
                if (cropDef) {
                    var gSec = cropDef.growth_sec || cropDef.growthSec || 20;
                    var rate = tile.watered ? 1.0 : 0.25;
                    if (tile.fertilized) rate *= 1.5;

                    tile.growthProgress += (dt / gSec) * rate;
                    if (tile.growthProgress >= 1.0) {
                        tile.stage = 3;
                        tile.growthProgress = 1.0;
                    } else if (tile.growthProgress >= 0.5) {
                        tile.stage = 2;
                    } else {
                        tile.stage = 1;
                    }
                }
            }
        });

        // 3. Advance Livestock
        State.animals.forEach(function (a) {
            if (a.fed && !a.produceReady) {
                var aDef = CONFIG.animals[a.type];
                if (aDef) {
                    var pSec = aDef.produce_sec || aDef.produceSec || 40;
                    a.progress += dt / pSec;
                    if (a.progress >= 1.0) {
                        a.progress = 1.0;
                        a.produceReady = true;
                    }
                }
            }
        });

        // 4. Render Everything
        var w = (canvas.width / dpr) || 600;
        var h = (canvas.height / dpr) || 400;
        ctx.clearRect(0, 0, w, h);

        var origin = getGridOrigin();
        var hoverTile = getTileAtPoint(mousePos.x, mousePos.y);

        drawEnvironment(origin);

        State.grid.forEach(function (tile) {
            drawTile(tile, origin, hoverTile);
        });

        drawAnimals();
        drawLightingAndWeather();
        drawPopupsAndParticles();

        if (now - lastSecond > 3000) {
            lastSecond = now;
            updateHUD();
            State.saveToBackend();
        }

        animId = requestAnimationFrame(gameLoop);
    }

    // Cleanup on window unload or module destruction
    function cleanup() {
        isRunning = false;
        if (animId) {
            cancelAnimationFrame(animId);
            animId = null;
        }
        window.removeEventListener('resize', resizeCanvas);
    }
    window.addEventListener('pagehide', cleanup);
    window.addEventListener('beforeunload', cleanup);

    // Initialize Game via backend fetch
    State.loadFromBackend(function () {
        resizeCanvas();
        updateHUD();
        animId = requestAnimationFrame(gameLoop);
    });

})();
