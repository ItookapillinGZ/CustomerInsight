<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const isScrolled = ref(false)
const navOpen = ref(false)
const bookingModal = ref(null)
const submitted = ref(false)
const activeCategory = ref('signature')
const showToast = ref(false)
let observer
let toastTimer

const categories = [
  { id: 'signature', label: '招牌菜' },
  { id: 'seasonal', label: '时令菜' },
  { id: 'tasting', label: '品鉴套餐' },
]

const menuData = {
  signature: [
    ['六时慢炖黑豚肉', '陈皮 · 菌菇 · 手工豆腐', '¥ 168', '慢火 · 六时辰'],
    ['松茸鸡汤狮子头', '云南松茸 · 清远鸡 · 马蹄', '¥ 138', '山野 · 鲜香'],
    ['青花椒蒸东海黄鱼', '鲜花椒 · 雪菜 · 手工年糕', '¥ 228', '江海 · 清润'],
  ],
  seasonal: [
    ['桂花酒酿蒸芋艿', '金桂 · 手作酒酿 · 荔浦芋', '¥ 88', '秋意 · 温润'],
    ['蟹粉手拆豆腐', '大闸蟹 · 盐卤豆腐 · 姜米', '¥ 158', '一口 · 江南'],
    ['板栗烧湖鸭', '迁西板栗 · 麻鸭 · 九年陈皮', '¥ 188', '丰收 · 甘香'],
  ],
  tasting: [
    ['山河七味品鉴', '七道式 · 当季精选 · 茶饮搭配', '¥ 688 / 位', '七味 · 一席'],
    ['榆庭九章', '九道式 · 主厨特选 · 餐酒搭配', '¥ 988 / 位', '九章 · 匠心'],
    ['素心五境', '五道式 · 时蔬 · 菌菇 · 谷物', '¥ 488 / 位', '素味 · 本真'],
  ],
}

const dishes = computed(() => menuData[activeCategory.value])
const today = new Date().toISOString().slice(0, 10)

function handleScroll() {
  isScrolled.value = window.scrollY > 40
}

function toggleNav() {
  navOpen.value = !navOpen.value
  document.body.style.overflow = navOpen.value ? 'hidden' : ''
}

function closeNav() {
  navOpen.value = false
  document.body.style.overflow = ''
}

function openBooking() {
  submitted.value = false
  closeNav()
  bookingModal.value?.showModal()
}

function submitBooking(event) {
  submitted.value = true
  event.currentTarget.reset()
}

function notifyMenu() {
  clearTimeout(toastTimer)
  showToast.value = true
  toastTimer = setTimeout(() => (showToast.value = false), 2400)
}

onMounted(async () => {
  handleScroll()
  window.addEventListener('scroll', handleScroll, { passive: true })
  await nextTick()
  observer = new IntersectionObserver(
    entries => entries.forEach(entry => entry.isIntersecting && entry.target.classList.add('visible')),
    { threshold: 0.12 },
  )
  document.querySelectorAll('.reveal').forEach(item => observer.observe(item))
})

onBeforeUnmount(() => {
  window.removeEventListener('scroll', handleScroll)
  observer?.disconnect()
  clearTimeout(toastTimer)
  document.body.style.overflow = ''
})
</script>

<template>
  <header class="site-header" :class="{ scrolled: isScrolled }">
    <a class="brand" href="#home" aria-label="榆庭首页"><span class="brand-mark">榆</span><span class="brand-text">榆庭<small>YUTING DINING</small></span></a>
    <button class="menu-toggle" aria-label="打开导航" :aria-expanded="navOpen" @click="toggleNav"><span></span><span></span></button>
    <nav class="nav" :class="{ open: navOpen }">
      <a href="#story" @click="closeNav">关于榆庭</a><a href="#menu" @click="closeNav">当季菜单</a><a href="#space" @click="closeNav">餐厅空间</a><a href="#contact" @click="closeNav">到店指南</a>
    </nav>
    <button class="header-book" @click="openBooking">预订座位</button>
  </header>

  <main>
    <section id="home" class="hero">
      <div class="hero-shade"></div>
      <div class="hero-content">
        <p class="eyebrow">SEASONAL CHINESE CUISINE · SHANGHAI</p>
        <h1>一席山河<br />落于<span>杯箸之间</span></h1>
        <p class="hero-copy">遵循四时风物，以当代手法重述江南滋味。<br />每一道菜，都是一段关于土地与时间的叙事。</p>
        <div class="hero-actions"><button class="btn btn-primary" @click="openBooking">预订一席</button><a class="text-link" href="#menu">探索当季菜单 <span>→</span></a></div>
      </div>
      <div class="hero-meta"><div><small>营业时间</small><strong>午市 11:30—14:00</strong><strong>晚市 17:30—24:00</strong></div><div><small>地址</small><strong>上海市静安区愚园路 168 号</strong></div></div>
      <a class="scroll-cue" href="#story" aria-label="向下浏览"><span></span>SCROLL</a>
    </section>

    <section id="story" class="story section">
      <div class="section-label reveal"><span>01</span> OUR PHILOSOPHY</div>
      <div class="story-grid">
        <div class="story-title reveal"><p class="kicker">风物有时 · 滋味有根</p><h2>不止于一餐，<br />是与四季的相逢。</h2></div>
        <div class="story-copy reveal"><p class="lead">我们相信，真正动人的滋味，来自对时节的尊重与对食材的克制。</p><p>榆庭寻访江南各地的小型农场与手艺人，从春笋、夏荷，到秋蟹、冬笋，将每一季最鲜活的馈赠带回餐桌。主厨林知远以传统技法为骨，以现代审美为形，呈现熟悉又崭新的东方味道。</p><a class="under-link" href="#space">了解我们的故事 <span>→</span></a></div>
      </div>
      <div class="story-stats reveal"><div><strong>24</strong><span>节气灵感菜单</span></div><div><strong>18</strong><span>长期合作农场</span></div><div><strong>7</strong><span>年在地探索</span></div></div>
    </section>

    <section id="menu" class="menu-section">
      <div class="menu-intro section">
        <div class="section-label light reveal"><span>02</span> SEASONAL MENU</div>
        <div class="menu-head reveal"><div><p class="kicker">初秋 · 桂香渐起</p><h2>此刻，正当食</h2></div><p>菜单随节气微调，选用当日到店的新鲜食材。<br />以下为本季部分代表菜品。</p></div>
        <div class="menu-tabs reveal" role="tablist" aria-label="菜单分类">
          <button v-for="category in categories" :key="category.id" class="menu-tab" :class="{ active: activeCategory === category.id }" role="tab" :aria-selected="activeCategory === category.id" @click="activeCategory = category.id">{{ category.label }}</button>
        </div>
      </div>
      <div class="dishes section reveal">
        <article v-for="(dish, index) in dishes" :key="`${activeCategory}-${index}`" class="dish-card" :class="{ featured: index === 0 }">
          <div class="dish-number">0{{ index + 1 }}</div><div class="dish-art" :class="`art-${['one', 'two', 'three'][index]}`"><span>{{ dish[3] }}</span></div><div class="dish-info"><div><p>{{ dish[0] }}</p><small>{{ dish[1] }}</small></div><strong>{{ dish[2] }}</strong></div>
        </article>
      </div>
      <div class="menu-footer section"><button class="btn btn-outline" @click="notifyMenu">查看完整菜单 <span>→</span></button><small>菜品图片仅供参考，具体以当日供应为准</small></div>
    </section>

    <section id="space" class="space section">
      <div class="section-label reveal"><span>03</span> THE SPACE</div>
      <div class="space-grid">
        <div class="space-visual reveal"><div class="space-frame"><span>榆影入庭</span><small>安静、温暖、恰到好处</small></div></div>
        <div class="space-content reveal"><p class="kicker">一方静室 · 一席好味</p><h2>让相聚，自然发生。</h2><p>木、石、铜与手工织物构成安静的用餐空间。自然光在格栅间缓缓移动，城市的喧闹被留在门外。</p><ul><li><span>01</span><div><strong>临窗雅座</strong><small>2—4 位 · 适合日常小聚</small></div></li><li><span>02</span><div><strong>独立包间</strong><small>6—12 位 · 提供专属菜单</small></div></li><li><span>03</span><div><strong>主厨餐桌</strong><small>8 位 · 沉浸式品鉴体验</small></div></li></ul><button class="under-link" @click="openBooking">咨询私人宴席 <span>→</span></button></div>
      </div>
    </section>

    <section class="quote-strip"><p>“味至浓时，是克制；情至深处，是寻常。”</p><span>— 主厨 林知远</span></section>
    <section id="contact" class="visit section">
      <div class="visit-copy reveal"><div class="section-label"><span>04</span> VISIT US</div><p class="kicker">期待，与您相见</p><h2>来榆庭，吃一顿<br />属于此刻的饭。</h2><button class="btn btn-dark" @click="openBooking">立即预订</button></div>
      <div class="visit-details reveal"><div><small>地址 ADDRESS</small><p>上海市静安区愚园路 168 号<br />近乌鲁木齐北路</p><a href="https://maps.google.com" target="_blank" rel="noreferrer">查看地图 →</a></div><div><small>营业 HOURS</small><p>午市 11:30—14:00<br />晚市 17:30—24:00<br />周一店休</p></div><div><small>联系 CONTACT</small><p>021 6888 1688<br />hello@yuting.cn</p></div></div>
    </section>
  </main>

  <footer><a class="brand footer-brand" href="#home"><span class="brand-mark">榆</span><span class="brand-text">榆庭<small>YUTING DINING</small></span></a><p>以四季入味，以火候见心。</p><div class="socials"><a href="#">小红书</a><a href="#">微信公众号</a><a href="#">大众点评</a></div><small>© 2026 YUTING DINING. ALL RIGHTS RESERVED.</small></footer>

  <dialog ref="bookingModal" class="booking-modal" @click.self="bookingModal.close()">
    <button class="modal-close" aria-label="关闭" @click="bookingModal.close()">×</button>
    <template v-if="!submitted"><p class="kicker">RESERVATION</p><h2>预订一席</h2><p class="modal-note">留下您的信息，我们将在 30 分钟内与您确认。</p><form @submit.prevent="submitBooking"><label>称呼<input type="text" name="name" placeholder="您的姓名" required /></label><label>手机<input type="tel" name="phone" placeholder="联系电话" required /></label><div class="form-row"><label>日期<input type="date" name="date" :min="today" required /></label><label>人数<select name="guests"><option>2 位</option><option>3 位</option><option>4 位</option><option>5 位</option><option>6 位及以上</option></select></label></div><button class="btn btn-primary" type="submit">提交预订</button></form></template>
    <div v-else class="form-success vue-success"><span>✓</span><h3>已收到您的预订</h3><p>我们会尽快与您联系确认。</p></div>
  </dialog>
  <div class="toast" :class="{ show: showToast }">完整菜单即将上线，敬请期待</div>
</template>

<style scoped>
.vue-success { display: block; }
</style>
