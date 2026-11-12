"""Separate the TED buyer evidence from the reviewed outreach subject."""

EXCHANGE_SUBJECT = 'Austausch zu öffentlichen Ausschreibungen'


def validate_subject(mail):
    subject = mail.get('subject')
    review = mail.get('subject_review')
    if (not isinstance(subject, str) or not subject.strip() or len(subject) > 70
            or any(c in subject for c in ('\r', '\n', '\u2013', '\u2014'))):
        raise ValueError('Subject missing, too long or contains forbidden characters')
    if (not isinstance(review, dict) or review.get('understandable') is not True
            or review.get('fits_request') is not True
            or not isinstance(review.get('reason'), str) or not review['reason'].strip()):
        raise ValueError('Subject needs an explicit content review, separate from TED verification')
    if review.get('mode') == 'buyer':
        if subject in ('Ihre Zuschläge', 'Eure Zuschläge') or subject not in mail['body']:
            raise ValueError('Reviewed buyer subject must also identify the buyer in the body')
    elif review.get('mode') == 'exchange':
        buyer = mail.get('ted_buyer')
        if (subject != EXCHANGE_SUBJECT or not isinstance(buyer, str)
                or not buyer.strip() or buyer not in mail['body']):
            raise ValueError('Exchange subject requires the independently verified TED buyer in the body')
    else:
        raise ValueError('Subject review mode must be buyer or exchange')
