// コマンドの「コピー」ボタン
document.querySelectorAll('.cmd:not(.tree):not(.out)').forEach(function (box) {
  var b = document.createElement('button');
  b.type = 'button';
  b.textContent = 'コピー';
  b.addEventListener('click', function () {
    var t = box.querySelector('pre').innerText.replace(/\n$/, '');
    navigator.clipboard.writeText(t).then(function () {
      b.textContent = 'コピーしました';
      b.classList.add('ok');
      setTimeout(function () { b.textContent = 'コピー'; b.classList.remove('ok'); }, 2000);
    });
  });
  box.appendChild(b);
});
