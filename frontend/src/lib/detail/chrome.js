/* 详情页长页 chrome：锚点 scroll-spy、阅读进度条、横滑导航渐隐、回到顶部。
 * 单例生命周期：首次 renderDetail 时 ensureDetailChrome()，SPA 会话内常驻；
 * 所有回调都带「详情页在场」守卫，离开详情路由后自动失效——不绑 Vue 卸载钩子，
 * 与 charts.js 的 resize 监听同一模式（那里靠 StockDetailView 显式解绑，这里
 * 守卫即可：元素查不到就是不在场）。
 */
var bound = false;
var spy = null;

function ensureDetailChrome() {
  if (bound) return;
  bound = true;

  // 回到顶部：12k px 长页，滚到中下部后右下角浮出
  var btn = document.createElement('button');
  btn.id = 'detail-backtop';
  btn.type = 'button';
  btn.title = '回到顶部';
  btn.setAttribute('aria-label', '回到顶部');
  btn.textContent = '↑';
  btn.addEventListener('click', function () {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  document.body.appendChild(btn);

  function onScroll() {
    // 直执行不加 rAF 节流：全部是 O(1) DOM 操作（toggle/width），滚动事件本身已被
    // 浏览器按帧对齐；rAF 在个别内嵌浏览器（IAB 后台页签）不触发，会让整组 chrome 假死
    var detail = document.getElementById('stock-detail');
    var on = detail && detail.style.display !== 'none';
    var y = window.scrollY || document.documentElement.scrollTop;
    btn.classList.toggle('show', !!on && y > window.innerHeight * 1.2);
    if (!on) return;
    // 阅读进度：详情体从顶部露出到滚过末尾，0→100%
    var body = document.getElementById('stock-detail-body');
    var bar = document.querySelector('.va-progress');
    if (body && bar) {
      var top = body.offsetTop - 100;         // 顶栏(54) + 吸顶导航(~40) + 余量
      var span = body.offsetHeight - window.innerHeight + 200;
      var p = span > 0 ? Math.max(0, Math.min(1, (y - top) / span)) : 0;
      bar.style.width = (p * 100).toFixed(1) + '%';
    }
    // 横滑导航（移动端）右缘渐隐：右侧还有锚点才提示，滚到头撤掉
    var nav = document.querySelector('.va-nav');
    if (nav) {
      var scrollable = nav.scrollWidth > nav.clientWidth + 2;
      var atEnd = nav.scrollLeft + nav.clientWidth >= nav.scrollWidth - 4;
      nav.classList.toggle('nav-more', scrollable && !atEnd);
      nav.classList.toggle('nav-end', scrollable && atEnd);
    }
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
}

function retargetDetailSpy() {
  // 重渲染后 section 集合变化：断开上一轮再重挂（spy 单例存模块变量）
  if (spy) spy.disconnect();
  spy = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      var id = en.target.id;
      document.querySelectorAll('.va-nav a').forEach(function (a) {
        a.classList.toggle('active', a.getAttribute('data-scroll') === id);
      });
    });
  }, { rootMargin: '-92px 0px -62% 0px', threshold: 0 });
  document.querySelectorAll('#stock-detail-body section[id^="sec-"]').forEach(function (s) {
    spy.observe(s);
  });
}

export { ensureDetailChrome, retargetDetailSpy };
