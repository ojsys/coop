"""Publish a starting Terms and Conditions at /terms.

The route has existed since the legal pages did, but showed "not published"
because nothing was ever pasted in. This seeds a complete first version that
the legal team then edits on the platform's Legal pages screen (or in the
admin). It never touches a ``terms`` document that already exists: whatever
the legal team has written wins.
"""
from django.db import migrations

TITLE = "Terms and Conditions"

BODY = """These terms govern your use of CooperativeOS, operated by Startup Ripple ("we", "us"). By creating an account, signing up a cooperative, joining one, or referring one, you agree to them. If you do not agree, please do not use the platform.

## 1. What CooperativeOS is

CooperativeOS is software that helps cooperative societies run their affairs: a member register, contributions and savings, loans, an append-only ledger, meetings and voting, and communication with members. We provide the software. Each cooperative remains responsible for its own decisions, its records and its dealings with its members.

## 2. Accounts

- You must give accurate information and keep it up to date.
- Keep your password private. You are responsible for what happens under your account.
- Tell us at once if you believe your account has been used without your permission.
- You must be at least 18, or the age of majority where you live, to create an account.

## 3. Cooperatives on the platform

The person who signs a cooperative up confirms that they are authorised to act for it. The cooperative's officers are responsible for:

- the accuracy of what is recorded, including members, contributions, loans and decisions;
- obtaining any consent their members' data requires, and following their own bye-laws;
- deciding who may hold office and what each role may do on the platform.

Before the platform moves money for a cooperative, we verify who runs it (KYC). We may ask for documents at any time and may pause money movement while a check is outstanding.

## 4. Free trial, plans and billing

- A new cooperative starts on a free trial. Nothing is charged during the trial.
- After the trial, the cooperative pays the price of its plan, shown on our pricing page and on each invoice. Plans are priced by the number of active people the cooperative serves.
- Invoices are payable by the due date shown. If an invoice stays unpaid, we may limit or suspend access until it is paid. A cooperative's records are not deleted for late payment.
- Prices may change. We will give at least 30 days' notice of a change to a cooperative's plan price.
- Fees already paid are not refundable, except where the law requires otherwise.

## 5. Money and payments

- CooperativeOS is not a bank and does not hold members' money. Payments made through the platform are processed by licensed payment providers, such as Paystack, and settle into the cooperative's own bank account.
- Payment providers' own terms also apply to payments made through them.
- The cooperative is responsible for the money it collects, lends and pays out, and for approving payments in line with its own rules.
- We are not responsible for delays or failures caused by banks or payment providers, though we will help investigate.

## 6. Members

Members use the platform at their cooperative's invitation. A member's relationship with their cooperative — their savings, loans, dividends and rights — is governed by that cooperative's bye-laws and by law, not by us. Questions about a balance, a loan or a decision should go first to the cooperative's officers.

## 7. Refer and earn

Anyone with an account may join our referral program and share a personal referral link.

- **Earning.** When a cooperative signs up through your link, you earn the commission shown on your referral page, as a share of each invoice the cooperative pays, for the number of months shown, counted from its first paid invoice. A free trial earns nothing because nothing is paid.
- **Terms are fixed per referral.** Each referral keeps the commission terms in force on the day the cooperative signed up. We may change the terms for future referrals, or close the program to new referrers, at any time.
- **One referrer per cooperative.** A cooperative can be credited to one referrer only. You cannot earn from a cooperative you founded or hold office in.
- **Payouts.** You may ask to withdraw your balance once it reaches the minimum shown on your referral page. We pay by bank transfer to the account you give us; you are responsible for its accuracy. Payouts are normally made within 14 days of a request.
- **Refunds and reversals.** If a payment that earned a commission is refunded or reversed, we may void that commission.
- **Fair use.** Spam, misleading claims about CooperativeOS, fake or duplicate signups, and any other abuse of the program are not allowed. We may withhold commissions, suspend a referral account, or end a referrer's participation where we reasonably suspect abuse.
- **Taxes.** You are responsible for any tax on what you earn.
- Referrers are independent. Nothing in the program makes you our employee, agent or partner, and you may not make promises on our behalf.

## 8. Acceptable use

You must not:

- use the platform for anything unlawful, including fraud or money laundering;
- try to access another cooperative's data, or anyone else's account;
- disrupt, overload, probe or reverse-engineer the platform;
- upload malicious code, or content that infringes someone else's rights.

## 9. Your data

How we collect and use personal data is explained in our [Privacy Notice](/privacy). A cooperative's records belong to the cooperative. We process them to provide the service, in line with the Nigeria Data Protection Act and other applicable law. A cooperative can export its records, and members can ask for a copy of their data or for it to be deleted, subject to records the law requires us or the cooperative to keep.

## 10. Our software

The platform, its design and its code belong to Startup Ripple. You may use them only to use the service. Feedback you send us may be used to improve CooperativeOS without obligation to you.

## 11. Availability and changes to the service

We work to keep CooperativeOS available and secure, but we cannot promise it will be uninterrupted or error-free. We may change, add or remove features. Where a change materially reduces what a paying cooperative gets, we will tell it in advance.

## 12. Liability

To the extent the law allows:

- the platform is provided "as is";
- we are not liable for indirect or consequential losses, or for loss of profit, revenue or data caused by events outside our reasonable control;
- our total liability to a cooperative in any 12 months is limited to the fees it paid us in that period.

Nothing in these terms limits liability that cannot be limited by law.

## 13. Suspension and ending

You may stop using the platform at any time. A cooperative may close its account by contacting us. We may suspend or close an account that breaches these terms, that has an unpaid invoice, or where the law requires it. Where we can, we will give notice and a chance to put things right first. After closure, we keep records only as long as the law requires.

## 14. Changes to these terms

We may update these terms. The date at the top shows when they last changed. For significant changes we will give notice by email or in the app before they take effect. Continuing to use the platform after that means you accept the updated terms.

## 15. Governing law

These terms are governed by the laws of the Federal Republic of Nigeria. Disputes go first to good-faith discussion; failing that, to the courts of Nigeria.

## 16. Contact

Questions about these terms: [info@mycooperativeos.com](mailto:info@mycooperativeos.com).
"""


def seed(apps, schema_editor):
    LegalDocument = apps.get_model("platform_admin", "LegalDocument")
    if LegalDocument.objects.filter(slug="terms").exists():
        return
    LegalDocument.objects.create(slug="terms", title=TITLE, body=BODY,
                                 published=True, order=1)


def unseed(apps, schema_editor):
    LegalDocument = apps.get_model("platform_admin", "LegalDocument")
    LegalDocument.objects.filter(slug="terms", body=BODY).delete()


class Migration(migrations.Migration):
    dependencies = [("platform_admin", "0033_platform_email")]
    operations = [migrations.RunPython(seed, unseed)]
