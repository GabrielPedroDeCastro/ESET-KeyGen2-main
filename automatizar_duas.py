"""Run both trial workflows and recover saved accounts without manual browser clicks."""
import argparse
import datetime
import json
import logging
import os
import re
from pathlib import Path
from selenium.common.exceptions import TimeoutException

ROOT = Path(__file__).resolve().parent
PRODUCTS = {
    'home': ('ESET HOME Security Premium', 'ESET HOME'),
    'business': ('ESET Small Business Security', 'SMALL BUSINESS'),
}
STATE_PATH = ROOT / 'estado_licencas.json'
RESULTS = ROOT / 'resultados'
SEPARATOR = '-' * 49


def read_state():
    state = json.loads(STATE_PATH.read_text(encoding='utf-8')) if STATE_PATH.exists() else {}
    for source in sorted(ROOT.glob('* - ESET KEYS.txt'), key=lambda p: p.stat().st_mtime):
        for block in source.read_text(encoding='utf-8').split(SEPARATOR):
            fields = dict(re.findall(r'^([^:\n]+): (.+)$', block, re.MULTILINE))
            for product, (name, _) in PRODUCTS.items():
                if fields.get('License Name', '').strip() == name and fields.get('License Key'):
                    previous = state.get(product, {})
                    state[product] = {
                        **previous,
                        'email': fields['Account Email'].strip(),
                        'password': fields['Account Password'].strip(),
                        'name': name,
                        'key': fields['License Key'].strip(),
                        'valid_until': fields['License Out Date'].strip(),
                        'status': 'complete',
                    }
    return state


def save_state(state):
    temporary = STATE_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(STATE_PATH)


def export(product, entry):
    RESULTS.mkdir(exist_ok=True)
    output = RESULTS / (PRODUCTS[product][0] + '.txt')
    text = '\n'.join([
        SEPARATOR, f"Account Email: {entry['email']}", f"Account Password: {entry['password']}",
        '', f"License Name: {entry['name']}", f"License Key: {entry['key']}",
        f"License Out Date: {entry['valid_until']}", SEPARATOR, '',
    ])
    output.write_text(text, encoding='utf-8')
    print(f'Arquivo salvo: {output}', flush=True)
    return output


def is_current(entry):
    if not entry.get('key'):
        return False
    match = re.search(r'\d{2}/\d{2}/\d{4}', entry.get('valid_until', ''))
    if not match:
        raise RuntimeError('A validade salva nao pode ser interpretada; use --verificar-online.')
    return datetime.datetime.strptime(match.group(), '%d/%m/%Y').date() >= datetime.date.today()


def first_visible(driver, selector):
    return next((e for e in driver.find_elements('css selector', selector)
                 if e.is_displayed() and e.is_enabled()), None)


def continue_button(driver):
    return next((e for e in driver.find_elements('tag name', 'button')
                 if e.is_displayed() and e.is_enabled()
                 and e.text.strip().casefold() in ('continue', 'continuar')), None)


def login(driver, entry, wait):
    driver.get('https://home.eset.com/subscriptions')
    wait.until(lambda d: 'login.eset.com' in d.current_url
               or first_visible(d, 'button[data-label="common-onboarding-menu-btn"]')
               or first_visible(d, 'button[data-label="common-side-menu-item-subscriptions"]'))
    if 'login.eset.com' not in driver.current_url:
        return
    wait.until(lambda d: first_visible(d, '[data-label="welcome-login-log-in-button"]')
               or first_visible(d, '#password'))
    welcome = first_visible(driver, '[data-label="welcome-login-log-in-button"]')
    if welcome:
        welcome.click()
    password_field = wait.until(lambda d: first_visible(d, '#password'))
    cookies = first_visible(driver, '#cc-decline')
    if cookies:
        cookies.click()
    email_field = first_visible(driver, '#email')
    if email_field:
        email_field.clear()
        email_field.send_keys(entry['email'])
    password_field.send_keys(entry['password'])
    def submit(d):
        cookie_button = first_visible(d, '#cc-decline')
        if cookie_button:
            cookie_button.click()
            return False
        button = first_visible(d, 'button[data-label="log-in-button"]')
        if button:
            button.click()
            return True
        return False
    wait.until(submit)
    wait.until(lambda d: 'home.eset.com' in d.current_url)


def finish_existing_onboarding(driver, product, wait):
    """Finish the welcome/device screens for an already activated subscription."""
    name = PRODUCTS[product][0]
    for _ in range(8):
        def screen(d):
            if first_visible(d, 'button[data-label="common-side-menu-item-subscriptions"]'):
                return 'done'
            if first_visible(d, 'button[data-label="onboarding-welcome-continue-btn"]'):
                return 'welcome'
            if first_visible(d, 'button[data-label^="choose-subscription-card-"]'):
                return 'subscription'
            if '/onboarding/download' in d.current_url and continue_button(d):
                return 'download'
            if d.find_elements('id', 'trial'):
                return 'new'
            return False
        stage = wait.until(screen)
        if stage in ('done', 'new'):
            return stage == 'done'
        previous = driver.find_element('tag name', 'body').text
        if stage == 'welcome':
            first_visible(driver, 'button[data-label="onboarding-welcome-continue-btn"]').click()
        elif stage == 'subscription':
            cards = driver.find_elements('css selector', 'button[data-label^="choose-subscription-card-"]')
            card = next((e for e in cards if e.is_displayed() and name in e.text), None)
            if card is None:
                raise RuntimeError('A conta nao possui a assinatura solicitada.')
            card.click()
            wait.until(continue_button).click()
        else:
            wait.until(continue_button).click()
        try:
            wait.until(lambda d: d.find_element('tag name', 'body').text != previous)
        except TimeoutException:
            if stage != 'download':
                raise
            driver.get('https://home.eset.com/subscriptions')
    raise RuntimeError('A apresentacao da ESET nao foi concluida.')


def run_product(product, state, verify_online):
    from selenium.webdriver import Chrome, ChromeOptions, ChromeService
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import StaleElementReferenceException, SessionNotCreatedException, ElementClickInterceptedException
    from modules.WebDriverInstaller import WebDriverInstaller, GOOGLE_CHROME
    from modules.EmailAPIs import EmailFakeAPI
    from modules.EsetTools import EsetRegister, EsetKeygen
    from modules.SharedTools import dataGenerator

    name, mode = PRODUCTS[product]
    entry = state.get(product, {})
    print(f'\n--- {name} ---', flush=True)
    if entry.get('key') and not verify_online:
        if not is_current(entry):
            raise RuntimeError('A licenca salva expirou; e necessario obter uma licenca valida na ESET.')
        print('Licenca valida ja salva.', flush=True)
        return export(product, entry)

    webdriver_path, browser_path = WebDriverInstaller(GOOGLE_CHROME).menu(True)
    options = ChromeOptions()
    options.page_load_strategy = 'eager'
    options.binary_location = browser_path
    options.add_argument('--lang=en-US')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    profile = ROOT / 'automation_browser' / product
    profile.mkdir(parents=True, exist_ok=True)
    options.add_argument('--user-data-dir=' + str(profile))
    try:
        driver = Chrome(options=options, service=ChromeService(webdriver_path))
    except SessionNotCreatedException:
        # Selenium Manager provides a fallback if Chrome updates during startup.
        driver = Chrome(options=options)
    driver.set_page_load_timeout(45)
    wait = WebDriverWait(driver, 35, ignored_exceptions=(StaleElementReferenceException, ElementClickInterceptedException))
    try:
        if entry.get('email'):
            login(driver, entry, wait)
            completed = finish_existing_onboarding(driver, product, wait)
            if not completed:
                EsetKeygen(None, driver, mode).sendRequestForKey()
        else:
            mail = EmailFakeAPI(driver)
            mail.init()
            entry = {'email': mail.email, 'password': dataGenerator(12), 'status': 'registering'}
            state[product] = entry
            save_state(state)
            registration = EsetRegister(mail, entry['password'], driver)
            registration.createAccount()
            entry['status'] = 'registered'
            save_state(state)
            registration.confirmAccount()
            entry['status'] = 'confirmed'
            save_state(state)
            keygen = EsetKeygen(mail, driver, mode)
            try:
                keygen.sendRequestForKey()
            except TimeoutException:
                # Resume this same account; never replace it with a new registration.
                driver.get('https://home.eset.com/subscriptions')
                if not finish_existing_onboarding(driver, product, wait):
                    raise
        actual_name, key, valid_until = EsetKeygen(None, driver, mode).getLD()
        if actual_name.strip() != name:
            raise RuntimeError('O produto da assinatura nao corresponde ao produto solicitado.')
        if not re.fullmatch(r'[A-Z0-9]{4}(?:-[A-Z0-9]{4}){4}', key.strip()):
            raise RuntimeError('A pagina nao forneceu uma chave valida.')
        entry.update(name=actual_name.strip(), key=key.strip(), valid_until=valid_until.strip(), status='complete')
        state[product] = entry
        save_state(state)
        if not is_current(entry):
            raise RuntimeError('A ESET informou uma licenca expirada.')
        return export(product, entry)
    finally:
        driver.quit()


def main():
    parser = argparse.ArgumentParser(description='Automatiza os dois produtos e salva suas licencas.')
    parser.add_argument('--verificar-online', action='store_true', help='Confere as contas e le novamente as chaves na ESET.')
    args = parser.parse_args()
    os.chdir(ROOT)
    logging.basicConfig(filename=ROOT / 'automatizacao.log', level=logging.INFO, encoding='utf-8')
    state = read_state()
    save_state(state)
    errors = []
    for product in PRODUCTS:
        try:
            run_product(product, state, args.verificar_online)
        except Exception as error:
            logging.exception('Falha no produto %s', product)
            print(f'Falha em {PRODUCTS[product][0]}: {type(error).__name__}: {error}', flush=True)
            errors.append(product)
    if errors:
        print(f'Consulte o registro: {ROOT / "automatizacao.log"}', flush=True)
        return 1
    combined = RESULTS / 'AS DUAS LICENCAS.txt'
    combined.write_text('\n'.join((RESULTS / (name + '.txt')).read_text(encoding='utf-8') for name, _ in PRODUCTS.values()), encoding='utf-8')
    print(f'\nConcluido. As duas licencas estao em: {combined}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
