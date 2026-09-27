/* The moot card: one take, built the way fanmoot.com draws it.
 *
 * Shared by every view that shows a take, and inlined beside the bridge, so
 * there is one renderer of a moot rather than one per view that each forget a
 * different thing. It builds DOM nodes and nothing else — no fetch, no markup
 * strings — and it is handed the bridge (`view`) for the three things a card
 * can do: open a link, react, and start a reply.
 *
 * Three rules every function here keeps, because each is a test:
 *
 * * **A take is text.** Everything a fan or a linked page wrote goes in through
 *   `textContent`. A moot is something a stranger typed, and markup built from
 *   it is a moot that can script the card it sits in.
 * * **Every image is named.** A crest is "{club} badge", a picture is the
 *   author's own description or "Image from {name}" (`S6-7`).
 * * **Links go through the host.** The sandbox has no navigation of its own;
 *   `ui/open-link` lets the host ask the person first.
 */
(function () {
  'use strict';

  //: Fanmoot's five, in the site's order. The word is always written beside
  //: the emoji — an emoji alone is a picture a screen reader guesses at.
  var REACTIONS = [
    ['respect', '👏'],
    ['heat', '🔥'],
    ['tactics', '🧠'],
    ['banter', '😂'],
    ['offside', '🚩'],
  ];

  var HEX = /^#[0-9a-fA-F]{6}$/;

  function el(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = value;
    return node;
  }

  function stop(event) {
    if (event && event.preventDefault) event.preventDefault();
    if (event && event.stopPropagation) event.stopPropagation();
  }

  /** The tool's own sentence from a CallToolResult, which is written for a fan. */
  function spoken(result) {
    var blocks = (result && result.content) || [];
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i] && blocks[i].type === 'text') return blocks[i].text;
    }
    return '';
  }

  /** A link the host opens. Still a real `<a href>`, so it can be copied. */
  function link(url, className, label, view) {
    var anchor = el('a', className, label);
    anchor.href = url;
    anchor.setAttribute('href', url);
    anchor.addEventListener('click', function (event) {
      stop(event);
      if (view) view.openLink(url);
    });
    return anchor;
  }

  function crest(url, name) {
    var img = document.createElement('img');
    img.className = 'crest';
    img.src = url;
    img.alt = name + ' badge';
    img.onerror = function () {
      img.hidden = true;
    };
    return img;
  }

  /** The fan's picture with their club's crest pinned to it. A picture the
   *  sandbox refuses (a third-party avatar) falls back to their initial. */
  function avatar(take) {
    var wrap = el('div', 'avatar-wrap');
    var initial = el('div', 'avatar', (take.name || take.handle || '?').charAt(0));
    initial.setAttribute('aria-hidden', 'true');
    if (take.avatar_url) {
      var img = document.createElement('img');
      img.className = 'avatar';
      img.src = take.avatar_url;
      // The name is written right beside it; saying it twice is noise.
      img.alt = '';
      img.onerror = function () {
        img.hidden = true;
        initial.hidden = false;
      };
      initial.hidden = true;
      wrap.appendChild(img);
    }
    wrap.appendChild(initial);
    if (take.club_crest_url && take.club) wrap.appendChild(crest(take.club_crest_url, take.club));
    return wrap;
  }

  /** "now", "4m", "3h", "2d", then a date — what a feed timestamp says. */
  function when(iso) {
    var at = new Date(iso);
    var seconds = Math.max(0, (Date.now() - at.getTime()) / 1000);
    if (!isFinite(seconds)) return '';
    if (seconds < 45) return 'now';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm';
    if (seconds < 86400) return Math.round(seconds / 3600) + 'h';
    if (seconds < 604800) return Math.round(seconds / 86400) + 'd';
    return at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  }

  function byline(take, view) {
    var line = el('div', 'byline');
    line.appendChild(
      take.profile_url ? link(take.profile_url, 'name', take.name || take.handle, view) : el('span', 'name', take.name || take.handle)
    );
    if (take.is_verified && !take.is_agent) {
      var tick = el('span', 'verified', '✓');
      tick.setAttribute('role', 'img');
      tick.setAttribute('aria-label', 'Verified');
      line.appendChild(tick);
    }
    if (take.is_agent) line.appendChild(el('span', 'pill', 'agent'));
    if (take.tier) line.appendChild(el('span', 'pill tier', take.tier));
    line.appendChild(el('span', 'faint', '@' + take.handle));
    if (take.club) line.appendChild(el('span', 'faint', '· ' + take.club));
    var stamp = when(take.posted_at);
    if (stamp) line.appendChild(el('span', 'faint', '· ' + stamp + (take.edited ? ' · edited' : '')));
    return line;
  }

  function body(take, clampAt) {
    var text = el('p', 'moot-body', take.body);
    if (!clampAt || !take.body || take.body.length <= clampAt) return [text];
    text.className = 'moot-body clamped';
    var more = el('button', 'text more', 'Show more');
    more.type = 'button';
    more.setAttribute('aria-expanded', 'false');
    more.addEventListener('click', function (event) {
      stop(event);
      text.className = 'moot-body';
      more.hidden = true;
      more.setAttribute('aria-expanded', 'true');
    });
    return [text, more];
  }

  function images(take) {
    var list = take.images || [];
    if (!list.length) return null;
    var grid = el('div', list.length === 1 ? 'images one' : 'images');
    list.forEach(function (image) {
      var img = document.createElement('img');
      img.src = image.url;
      img.alt = image.alt || 'Image from ' + (take.name || take.handle);
      if (image.width) img.setAttribute('width', image.width);
      if (image.height) img.setAttribute('height', image.height);
      img.setAttribute('loading', 'lazy');
      img.onerror = function () {
        img.hidden = true;
      };
      grid.appendChild(img);
    });
    return grid;
  }

  function linkCard(preview, view) {
    var card = link(preview.url, 'link-card', null, view);
    if (preview.image_url) {
      var img = document.createElement('img');
      img.src = preview.image_url;
      // The title is right under it and says what it is.
      img.alt = '';
      img.onerror = function () {
        img.hidden = true;
      };
      card.appendChild(img);
    }
    var words = el('div', 'link-text');
    if (preview.domain) words.appendChild(el('p', 'faint', preview.domain));
    words.appendChild(el('p', 'title', preview.title || preview.url));
    if (preview.description) words.appendChild(el('p', 'description muted', preview.description));
    card.appendChild(words);
    return card;
  }

  /** "Live", "FT", a date — what the chip says about where the match is. */
  function matchState(match) {
    if (match.status === 'live') return 'Live';
    if (match.status === 'finished') return 'FT';
    if (match.status === 'postponed') return 'Postponed';
    if (match.status === 'cancelled') return 'Cancelled';
    var at = new Date(match.kickoff_at);
    return isFinite(at.getTime())
      ? at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
      : '';
  }

  /** The way back to the match thread a take was posted under. The score is
   *  only ever one somebody confirmed — the server leaves it out otherwise. */
  function matchChip(match, view) {
    var scored = match.home_score != null && match.away_score != null;
    var fixture =
      match.home + ' ' + (scored ? match.home_score + '–' + match.away_score : 'v') + ' ' + match.away;
    var state = matchState(match);
    var chip = match.url ? link(match.url, 'match-chip', null, view) : el('span', 'match-chip');
    chip.setAttribute('aria-label', 'Match thread: ' + fixture + (state ? ', ' + state : ''));
    if (match.home_crest_url) chip.appendChild(crest(match.home_crest_url, match.home));
    chip.appendChild(el('span', 'fixture', fixture));
    if (match.away_crest_url) chip.appendChild(crest(match.away_crest_url, match.away));
    if (state) chip.appendChild(el('span', match.status === 'live' ? 'pill live' : 'faint', state));
    if (match.url) chip.appendChild(el('span', 'cta', 'Match thread ›'));
    return chip;
  }

  function quoteOf(quoted, view) {
    var box = quoted.url ? link(quoted.url, 'quote', null, view) : el('div', 'quote');
    var line = el('div', 'byline');
    line.appendChild(el('span', 'name', quoted.name || quoted.handle));
    line.appendChild(el('span', 'faint', '@' + quoted.handle));
    box.appendChild(line);
    if (quoted.body) box.appendChild(el('p', 'moot-body', quoted.body));
    return box;
  }

  /** The reaction row. Read-only it is the words and the counts; given a
   *  bridge, each is a button that calls `react_to_moot` as the fan — and the
   *  counts it shows afterwards are the ones the tool answered with, never a
   *  guess, because a reaction from a fan who is not connected did not land. */
  function reactionBar(take, view, status, interactive) {
    var bar = el('span', 'reactions');
    var state = { counts: take.reactions || {}, mine: null, open: false, busy: false };

    function draw() {
      bar.replaceChildren();
      REACTIONS.forEach(function (pair) {
        var kind = pair[0];
        var count = state.counts[kind] || 0;
        var chosen = state.mine === kind;
        if (!count && !chosen && !state.open) return;
        var label = count ? kind + ' ' + count : kind;
        var chip = el(interactive ? 'button' : 'span', 'chip');
        var emoji = el('span', null, pair[1]);
        emoji.setAttribute('aria-hidden', 'true');
        chip.appendChild(emoji);
        chip.appendChild(el('span', null, label));
        if (interactive) {
          chip.type = 'button';
          chip.disabled = state.busy;
          chip.setAttribute('aria-pressed', chosen ? 'true' : 'false');
          chip.setAttribute('data-act', 'react:' + kind);
          chip.addEventListener('click', function (event) {
            stop(event);
            react(kind);
          });
        }
        bar.appendChild(chip);
      });
      if (interactive && !state.open) {
        var more = el('button', 'chip', '+');
        more.type = 'button';
        more.setAttribute('aria-label', 'React');
        more.setAttribute('data-act', 'react-more');
        more.addEventListener('click', function (event) {
          stop(event);
          state.open = true;
          draw();
        });
        bar.appendChild(more);
      }
    }

    function react(kind) {
      state.busy = true;
      draw();
      settle(status, 'Reacting…');
      view
        .callTool('react_to_moot', { take: take.short_id || take.id, reaction: kind })
        .then(function (result) {
          if (result && result.isError) {
            settle(status, spoken(result) || 'That reaction did not land.', 'bad');
            return;
          }
          var payload = (result && result.structuredContent) || {};
          state.counts = payload.reactions || state.counts;
          state.mine = payload.reaction || null;
          state.open = false;
          settle(status, state.mine ? 'Reacted with ' + state.mine + '.' : 'Reaction cleared.', 'good');
        })
        .catch(function (error) {
          settle(status, (error && error.message) || 'That reaction did not land.', 'bad');
        })
        .then(function () {
          state.busy = false;
          draw();
        });
    }

    draw();
    return bar;
  }

  function settle(status, message, kind) {
    if (!status) return;
    status.textContent = message || '';
    status.className = 'card-status' + (kind ? ' ' + kind : '');
  }

  /**
   * One take as a card.
   *
   *   options.view      the bridge — links, reactions and replies need it
   *   options.react     reaction chips are buttons that call `react_to_moot`
   *   options.onReply   a Reply button that hands this take to the caller
   *   options.clamp     characters after which the body folds (0: never)
   *   options.tag       the element to build (`li` inside a list)
   *   options.alone     no divider above it: the only card in its view
   */
  function take(item, options) {
    options = options || {};
    var view = options.view || null;
    var card = el(options.tag || 'article', options.alone ? 'moot alone' : 'moot');
    if (item.club_colour && HEX.test(item.club_colour)) card.style.setProperty('--club', item.club_colour);

    card.appendChild(avatar(item));
    var main = el('div', 'moot-main');
    main.appendChild(byline(item, view));
    if (item.body) body(item, options.clamp == null ? 280 : options.clamp).forEach(function (node) {
      main.appendChild(node);
    });

    var pictures = images(item);
    if (pictures) main.appendChild(pictures);
    if (item.link) main.appendChild(linkCard(item.link, view));
    if (item.quote) main.appendChild(quoteOf(item.quote, view));
    if (item.match && options.match !== false) main.appendChild(matchChip(item.match, view));

    var status = el('p', 'card-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    var actions = el('div', 'moot-actions');
    var replies = item.reply_count || 0;
    var replyLabel = replies === 1 ? '1 reply' : replies + ' replies';
    if (options.onReply) {
      var reply = el('button', 'chip', 'Reply' + (replies ? ' · ' + replies : ''));
      reply.type = 'button';
      reply.setAttribute('aria-label', 'Reply to ' + (item.name || item.handle) + ', ' + replyLabel + ' so far');
      reply.setAttribute('data-act', 'reply:' + (item.short_id || item.id));
      reply.addEventListener('click', function (event) {
        stop(event);
        options.onReply(item);
      });
      actions.appendChild(reply);
    } else if (replies) {
      actions.appendChild(el('span', 'chip', replyLabel));
    }
    actions.appendChild(reactionBar(item, view, status, Boolean(options.react && view)));
    actions.appendChild(el('span', 'spacer'));
    if (item.url) actions.appendChild(link(item.url, 'open', 'Open on Fanmoot', view));
    main.appendChild(actions);
    main.appendChild(status);

    card.appendChild(main);
    return card;
  }

  window.fanmootCard = {
    el: el,
    link: link,
    crest: crest,
    spoken: spoken,
    settle: settle,
    when: when,
    matchChip: matchChip,
    take: take,
  };
})();
