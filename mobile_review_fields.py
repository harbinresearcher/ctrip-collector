"""Recover visibly labeled card fields from current and saved Android observations."""
import json
import logging
import re

logger = logging.getLogger(__name__)
SIGHT_COUNT_FIELDS = ('photo_count_display', 'helpful_count_display', 'thread_comment_count_display')
SCOPED_COUNT_FIELDS = {'sight': SIGHT_COUNT_FIELDS, 'restaurant': ('photo_count_display',)}
CARD_FIELDS = ('room_type', 'travel_type', 'visit_duration', 'verified_purchase_visible', 'hotel_reply',
               'helpful_count_display', 'media_tiles_visible', 'photo_count_display', 'thread_comment_count_display',
               'media_layout_unrecognized')
SHOPPING_RATING_FIELDS = {'商品': 'shopping_product_score', '环境': 'shopping_environment_score',
                          '服务': 'shopping_service_score'}
SHOPPING_CARD_FIELDS = (*SHOPPING_RATING_FIELDS.values(), 'shopping_reaction_count_display')
SHOPPING_SCORE_VALUE = r'(?:[0-4](?:\.[0-9])?|5(?:\.0)?)'
SHOPPING_SCORE = re.compile(rf'({SHOPPING_SCORE_VALUE})分')
SHOPPING_DIMENSION = re.compile(rf'(商品|环境|服务)\s*({SHOPPING_SCORE_VALUE})')
HELPFUL_ICON = '\U000f0c85'
HELPFUL_COUNT = re.compile(r'有用\s*([0-9]{1,10})')
TRAVEL_TYPES = {'家庭亲子', '朋友出游', '情侣夫妻', '情侣出游', '商务出差', '独自旅行',
                '独自出游', '独自一人', '家庭出游', '商务旅行', '代人预订'}


SIGHT_FOOTER = re.compile(
    r'(?:(?P<photo_count_display>[0-9]{1,9})图, )?'
    r'\U000f12c6, 有用(?P<helpful_count_display>[0-9]{1,9})?, '
    r'\ue951, 评论(?P<thread_comment_count_display>[0-9]{1,9})?'
    r'(?:, o_cct_comment_list_usage, 这条评价内容对你有用吗？, 有用, 无用, \ue940)?'
)


def sight_footer_counts(tail):
    match = SIGHT_FOOTER.fullmatch(tail)
    return {key: int(value) for key, value in match.groupdict().items() if value is not None} if match else {}


def sight_accessibility_counts(description, rating_through_body):
    """The card description starts with the same rating, labels and exact body text."""
    if not description or not rating_through_body:
        return {}
    prefix = ', '.join(rating_through_body) + ', '
    if not description.startswith(prefix):
        return {}
    return sight_footer_counts(description[len(prefix):])


def extract_shopping_fields(texts, body_index, rating):
    score_indices = [index for index, word in enumerate(texts[:body_index])
                     if (match := SHOPPING_SCORE.fullmatch(word.strip())) and f'{float(match[1]):.1f}' == rating]
    if not score_indices:
        return {}
    labels = [word.strip() for word in texts[score_indices[-1] + 1:body_index]]
    result = {}
    for dimension, key in SHOPPING_RATING_FIELDS.items():
        values = {match[2] for word in labels if (match := SHOPPING_DIMENSION.fullmatch(word)) and match[1] == dimension}
        if len(values) == 1:
            result[key] = values.pop()
    if '消费后评价' in labels:
        result['verified_purchase_visible'] = True
    footer = [word.strip() for word in texts[body_index + 1:]]
    counts = [count for icon, count in zip(footer, footer[1:]) if icon == '\ue956'
              and re.fullmatch(r'[0-9]{1,10}', count)]
    if len(counts) == 1:
        result['shopping_reaction_count_display'] = int(counts[0])
    return result


def extract_card_fields(kind, texts, content, rating):
    if kind not in ('hotel', 'sight', 'shopping') or not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        return {}
    if not content or texts.count(content) != 1:
        return {}
    body_index = texts.index(content)
    if kind == 'shopping':
        return extract_shopping_fields(texts, body_index, rating)
    fields = {}
    if rating and rating in texts[:body_index]:
        rating_index = max(index for index, text in enumerate(texts[:body_index]) if text == rating)
        metadata = [text.strip() for text in texts[rating_index + 1:body_index]]
        if kind == 'hotel' and metadata[:1] == ['分'] and metadata.count('|') == 1 and metadata.count('\ue944') == 1:
            # The hotel renderer exposes room and trip labels as individual glyphs, separated by an icon and a bar.
            icon, separator = metadata.index('\ue944'), metadata.index('|')
            room = ''.join(metadata[1:icon])
            trip = ''.join(metadata[separator + 1:])
            if icon + 1 == separator and 1 <= len(room) <= 48 and re.fullmatch(r'[\w（）()·+\- ]+', room):
                fields['room_type'] = room
                if trip in TRAVEL_TYPES:
                    fields['travel_type'] = trip
        if kind == 'sight':
            trips = set(metadata) & TRAVEL_TYPES
            if len(trips) == 1:
                fields['travel_type'] = next(iter(trips))
            durations = {match[1].strip() for text in metadata
                         if (match := re.fullmatch(r'游玩时长\s*[:：]\s*(.{1,32})', text))}
            if len(durations) == 1 and any(durations):
                fields['visit_duration'] = next(iter(durations))
            if '消费后评价' in metadata:
                fields['verified_purchase_visible'] = True
    if kind == 'sight':
        fields.update(sight_footer_counts(', '.join(texts[body_index + 1:])))
    if kind == 'hotel':
        replies = {match[1].strip() for text in texts[body_index + 1:]
                   if (match := re.fullmatch(r'(?:酒店|商家)回复\s*[:：]\s*(.+)', text.strip(), re.S))}
        if len(replies) == 1 and any(replies):
            fields['hotel_reply'] = next(iter(replies))
        # The observed card footer puts the helpful icon immediately before its own label.
        # A clipped or unnumbered label cannot establish zero votes.
        tail = texts[body_index + 1:]
        controls = [label.strip() for icon, label in zip(tail, tail[1:]) if icon == HELPFUL_ICON]
        if len(controls) == 1 and (count := HELPFUL_COUNT.fullmatch(controls[0])):
            fields['helpful_count_display'] = int(count[1])
    return fields


def resolve_card_fields(db, kind, key, content, raw_texts, rating, observed, *, media_available=False,
                        sight_counts_available=False):
    def decode(texts, score):
        try:
            return extract_card_fields(kind, json.loads(texts), content, score)
        except (TypeError, ValueError):
            return {}

    fields = decode(raw_texts, rating)
    if observed and key:
        # Newer nonempty observations win; a reply scrolling out of view does not erase it.
        available = {}
        expected = {'travel_type', 'visit_duration', 'verified_purchase_visible', *SIGHT_COUNT_FIELDS}
        if kind == 'hotel':
            expected = {'room_type', 'travel_type', 'hotel_reply', 'helpful_count_display'}
        elif kind == 'shopping':
            expected = {'verified_purchase_visible', *SHOPPING_CARD_FIELDS}
        rows = db.execute('''SELECT raw_texts,rating FROM reviews_scoped_observations
                            WHERE review_key=? ORDER BY observation_id DESC''', (key,))
        for observed_texts, observed_rating in rows:
            for field, value in decode(observed_texts, observed_rating).items():
                available.setdefault(field, value)
            if expected.issubset(available):
                break
        fields.update(available)
    if media_available and kind in ('hotel', 'restaurant') and key:
        row = db.execute('SELECT visible_tiles FROM reviews_scoped_media WHERE review_key=?', (key,)).fetchone()
        if row:
            fields['media_tiles_visible'] = row[0]
    if sight_counts_available and key and (count_fields := SCOPED_COUNT_FIELDS.get(kind)):
        counts = db.execute('SELECT field, value FROM reviews_scoped_sight_counts WHERE review_key=?', (key,))
        fields.update((field, value) for field, value in counts if field in count_fields)
    return fields
