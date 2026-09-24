"""
Static reference data: the country catalogue, per-country subdivisions, and
the cooperative-type list.

Location is captured in three levels, which is the most any country here
needs and the fewest that works everywhere:

    country  ->  region     ->  locality
    Nigeria      State          LGA
    Kenya        County         Sub-county
    India        State          District
    (default)    State/Province City/District

The column names on ``tenants.Cooperative`` are still ``state`` and ``lga``
for the middle and last levels. That is deliberate: renaming them would touch
every console form, both serializers and the ``by_state`` analytics rollup
without changing behaviour. What varies per country is the *label*, supplied
by :data:`LOCATION_LABELS`, not the storage.

Only countries listed in :data:`SUBDIVISIONS` offer dropdowns; everywhere else
the two lower levels are free text, which is honest — we would rather take a
typed province name than pretend to a list we have not verified.
"""
from __future__ import annotations

# Cooperative types offered on the platform (feeds the "Type" dropdown).
# Deliberately spans the vocabulary used in different markets: a Kenyan SACCO,
# a North American credit union and a Nigerian thrift & credit society are all
# the same animal, and each should be able to find its own word here.
COOP_TYPES = [
    "Multipurpose",
    "Thrift & Credit",
    "SACCO (Savings & Credit)",
    "Credit Union",
    "Farmers / Agricultural",
    "Marketing & Supply",
    "Consumer",
    "Housing",
    "Investment",
    "Worker / Producer",
    "Artisan / Trade",
    "Transport",
    "Staff / Workers",
    "Community / Town",
    "Women / Youth",
    "Fishery",
    "Energy / Utility",
]

# state -> (capital, [LGAs])
NIGERIA: dict[str, tuple[str, list[str]]] = {
    "Abia": ("Umuahia", [
        "Aba North", "Aba South", "Arochukwu", "Bende", "Ikwuano",
        "Isiala Ngwa North", "Isiala Ngwa South", "Isuikwuato", "Obi Ngwa",
        "Ohafia", "Osisioma", "Ugwunagbo", "Ukwa East", "Ukwa West",
        "Umuahia North", "Umuahia South", "Umu Nneochi",
    ]),
    "Adamawa": ("Yola", [
        "Demsa", "Fufore", "Ganye", "Gayuk", "Gombi", "Grie", "Hong", "Jada",
        "Lamurde", "Madagali", "Maiha", "Mayo Belwa", "Michika",
        "Mubi North", "Mubi South", "Numan", "Shelleng", "Song", "Toungo",
        "Yola North", "Yola South",
    ]),
    "Akwa Ibom": ("Uyo", [
        "Abak", "Eastern Obolo", "Eket", "Esit Eket", "Essien Udim",
        "Etim Ekpo", "Etinan", "Ibeno", "Ibesikpo Asutan", "Ibiono Ibom",
        "Ika", "Ikono", "Ikot Abasi", "Ikot Ekpene", "Ini", "Itu", "Mbo",
        "Mkpat Enin", "Nsit Atai", "Nsit Ibom", "Nsit Ubium", "Obot Akara",
        "Okobo", "Onna", "Oron", "Oruk Anam", "Udung Uko", "Ukanafun",
        "Uruan", "Urue-Offong/Oruko", "Uyo",
    ]),
    "Anambra": ("Awka", [
        "Aguata", "Anambra East", "Anambra West", "Anaocha", "Awka North",
        "Awka South", "Ayamelum", "Dunukofia", "Ekwusigo", "Idemili North",
        "Idemili South", "Ihiala", "Njikoka", "Nnewi North", "Nnewi South",
        "Ogbaru", "Onitsha North", "Onitsha South", "Orumba North",
        "Orumba South", "Oyi",
    ]),
    "Bauchi": ("Bauchi", [
        "Alkaleri", "Bauchi", "Bogoro", "Damban", "Darazo", "Dass", "Gamawa",
        "Ganjuwa", "Giade", "Itas/Gadau", "Jama'are", "Katagum", "Kirfi",
        "Misau", "Ningi", "Shira", "Tafawa Balewa", "Toro", "Warji", "Zaki",
    ]),
    "Bayelsa": ("Yenagoa", [
        "Brass", "Ekeremor", "Kolokuma/Opokuma", "Nembe", "Ogbia", "Sagbama",
        "Southern Ijaw", "Yenagoa",
    ]),
    "Benue": ("Makurdi", [
        "Ado", "Agatu", "Apa", "Buruku", "Gboko", "Guma", "Gwer East",
        "Gwer West", "Katsina-Ala", "Konshisha", "Kwande", "Logo", "Makurdi",
        "Obi", "Ogbadibo", "Ohimini", "Oju", "Okpokwu", "Otukpo", "Tarka",
        "Ukum", "Ushongo", "Vandeikya",
    ]),
    "Borno": ("Maiduguri", [
        "Abadam", "Askira/Uba", "Bama", "Bayo", "Biu", "Chibok", "Damboa",
        "Dikwa", "Gubio", "Guzamala", "Gwoza", "Hawul", "Jere", "Kaga",
        "Kala/Balge", "Konduga", "Kukawa", "Kwaya Kusar", "Mafa", "Magumeri",
        "Maiduguri", "Marte", "Mobbar", "Monguno", "Ngala", "Nganzai",
        "Shani",
    ]),
    "Cross River": ("Calabar", [
        "Abi", "Akamkpa", "Akpabuyo", "Bakassi", "Bekwarra", "Biase",
        "Boki", "Calabar Municipal", "Calabar South", "Etung", "Ikom",
        "Obanliku", "Obubra", "Obudu", "Odukpani", "Ogoja", "Yakurr", "Yala",
    ]),
    "Delta": ("Asaba", [
        "Aniocha North", "Aniocha South", "Bomadi", "Burutu", "Ethiope East",
        "Ethiope West", "Ika North East", "Ika South", "Isoko North",
        "Isoko South", "Ndokwa East", "Ndokwa West", "Okpe", "Oshimili North",
        "Oshimili South", "Patani", "Sapele", "Udu", "Ughelli North",
        "Ughelli South", "Ukwuani", "Uvwie", "Warri North", "Warri South",
        "Warri South West",
    ]),
    "Ebonyi": ("Abakaliki", [
        "Abakaliki", "Afikpo North", "Afikpo South", "Ebonyi", "Ezza North",
        "Ezza South", "Ikwo", "Ishielu", "Ivo", "Izzi", "Ohaozara", "Ohaukwu",
        "Onicha",
    ]),
    "Edo": ("Benin City", [
        "Akoko-Edo", "Egor", "Esan Central", "Esan North-East", "Esan South-East",
        "Esan West", "Etsako Central", "Etsako East", "Etsako West", "Igueben",
        "Ikpoba Okha", "Oredo", "Orhionmwon", "Ovia North-East",
        "Ovia South-West", "Owan East", "Owan West", "Uhunmwonde",
    ]),
    "Ekiti": ("Ado-Ekiti", [
        "Ado Ekiti", "Efon", "Ekiti East", "Ekiti South-West", "Ekiti West",
        "Emure", "Gbonyin", "Ido Osi", "Ijero", "Ikere", "Ikole", "Ilejemeje",
        "Irepodun/Ifelodun", "Ise/Orun", "Moba", "Oye",
    ]),
    "Enugu": ("Enugu", [
        "Aninri", "Awgu", "Enugu East", "Enugu North", "Enugu South",
        "Ezeagu", "Igbo Etiti", "Igbo Eze North", "Igbo Eze South",
        "Isi Uzo", "Nkanu East", "Nkanu West", "Nsukka", "Oji River",
        "Udenu", "Udi", "Uzo Uwani",
    ]),
    "FCT": ("Abuja", [
        "Abaji", "Bwari", "Gwagwalada", "Kuje", "Kwali", "Municipal Area Council",
    ]),
    "Gombe": ("Gombe", [
        "Akko", "Balanga", "Billiri", "Dukku", "Funakaye", "Gombe", "Kaltungo",
        "Kwami", "Nafada", "Shongom", "Yamaltu/Deba",
    ]),
    "Imo": ("Owerri", [
        "Aboh Mbaise", "Ahiazu Mbaise", "Ehime Mbano", "Ezinihitte", "Ideato North",
        "Ideato South", "Ihitte/Uboma", "Ikeduru", "Isiala Mbano", "Isu",
        "Mbaitoli", "Ngor Okpala", "Njaba", "Nkwerre", "Nwangele", "Obowo",
        "Oguta", "Ohaji/Egbema", "Okigwe", "Onuimo", "Orlu", "Orsu",
        "Oru East", "Oru West", "Owerri Municipal", "Owerri North",
        "Owerri West",
    ]),
    "Jigawa": ("Dutse", [
        "Auyo", "Babura", "Biriniwa", "Birnin Kudu", "Buji", "Dutse", "Gagarawa",
        "Garki", "Gumel", "Guri", "Gwaram", "Gwiwa", "Hadejia", "Jahun",
        "Kafin Hausa", "Kaugama", "Kazaure", "Kiri Kasama", "Kiyawa", "Maigatari",
        "Malam Madori", "Miga", "Ringim", "Roni", "Sule Tankarkar", "Taura",
        "Yankwashi",
    ]),
    "Kaduna": ("Kaduna", [
        "Birnin Gwari", "Chikun", "Giwa", "Igabi", "Ikara", "Jaba", "Jema'a",
        "Kachia", "Kaduna North", "Kaduna South", "Kagarko", "Kajuru", "Kaura",
        "Kauru", "Kubau", "Kudan", "Lere", "Makarfi", "Sabon Gari", "Sanga",
        "Soba", "Zangon Kataf", "Zaria",
    ]),
    "Kano": ("Kano", [
        "Ajingi", "Albasu", "Bagwai", "Bebeji", "Bichi", "Bunkure", "Dala",
        "Dambatta", "Dawakin Kudu", "Dawakin Tofa", "Doguwa", "Fagge", "Gabasawa",
        "Garko", "Garun Mallam", "Gaya", "Gezawa", "Gwale", "Gwarzo", "Kabo",
        "Kano Municipal", "Karaye", "Kibiya", "Kiru", "Kumbotso", "Kunchi",
        "Kura", "Madobi", "Makoda", "Minjibir", "Nasarawa", "Rano", "Rimin Gado",
        "Rogo", "Shanono", "Sumaila", "Takai", "Tarauni", "Tofa", "Tsanyawa",
        "Tudun Wada", "Ungogo", "Warawa", "Wudil",
    ]),
    "Katsina": ("Katsina", [
        "Bakori", "Batagarawa", "Batsari", "Baure", "Bindawa", "Charanchi",
        "Dandume", "Danja", "Dan Musa", "Daura", "Dutsi", "Dutsin Ma", "Faskari",
        "Funtua", "Ingawa", "Jibia", "Kafur", "Kaita", "Kankara", "Kankia",
        "Katsina", "Kurfi", "Kusada", "Mai'Adua", "Malumfashi", "Mani",
        "Mashi", "Matazu", "Musawa", "Rimi", "Sabuwa", "Safana", "Sandamu",
        "Zango",
    ]),
    "Kebbi": ("Birnin Kebbi", [
        "Aleiro", "Arewa Dandi", "Argungu", "Augie", "Bagudo", "Birnin Kebbi",
        "Bunza", "Dandi", "Fakai", "Gwandu", "Jega", "Kalgo", "Koko/Besse",
        "Maiyama", "Ngaski", "Sakaba", "Shanga", "Suru", "Wasagu/Danko", "Yauri",
        "Zuru",
    ]),
    "Kogi": ("Lokoja", [
        "Adavi", "Ajaokuta", "Ankpa", "Bassa", "Dekina", "Ibaji", "Idah",
        "Igalamela Odolu", "Ijumu", "Kabba/Bunu", "Kogi", "Lokoja", "Mopa Muro",
        "Ofu", "Ogori/Magongo", "Okehi", "Okene", "Olamaboro", "Omala",
        "Yagba East", "Yagba West",
    ]),
    "Kwara": ("Ilorin", [
        "Asa", "Baruten", "Edu", "Ekiti", "Ifelodun", "Ilorin East",
        "Ilorin South", "Ilorin West", "Irepodun", "Isin", "Kaiama", "Moro",
        "Offa", "Oke Ero", "Oyun", "Pategi",
    ]),
    "Lagos": ("Ikeja", [
        "Agege", "Ajeromi-Ifelodun", "Alimosho", "Amuwo-Odofin", "Apapa",
        "Badagry", "Epe", "Eti Osa", "Ibeju-Lekki", "Ifako-Ijaiye", "Ikeja",
        "Ikorodu", "Kosofe", "Lagos Island", "Lagos Mainland", "Mushin",
        "Ojo", "Oshodi-Isolo", "Shomolu", "Surulere",
    ]),
    "Nasarawa": ("Lafia", [
        "Akwanga", "Awe", "Doma", "Karu", "Keana", "Keffi", "Kokona", "Lafia",
        "Nasarawa", "Nasarawa Egon", "Obi", "Toto", "Wamba",
    ]),
    "Niger": ("Minna", [
        "Agaie", "Agwara", "Bida", "Borgu", "Bosso", "Chanchaga", "Edati",
        "Gbako", "Gurara", "Katcha", "Kontagora", "Lapai", "Lavun", "Magama",
        "Mariga", "Mashegu", "Mokwa", "Munya", "Paikoro", "Rafi", "Rijau",
        "Shiroro", "Suleja", "Tafa", "Wushishi",
    ]),
    "Ogun": ("Abeokuta", [
        "Abeokuta North", "Abeokuta South", "Ado-Odo/Ota", "Egbado North",
        "Egbado South", "Ewekoro", "Ifo", "Ijebu East", "Ijebu North",
        "Ijebu North East", "Ijebu Ode", "Ikenne", "Imeko Afon", "Ipokia",
        "Obafemi Owode", "Odeda", "Odogbolu", "Ogun Waterside", "Remo North",
        "Sagamu",
    ]),
    "Ondo": ("Akure", [
        "Akoko North-East", "Akoko North-West", "Akoko South-East",
        "Akoko South-West", "Akure North", "Akure South", "Ese Odo", "Idanre",
        "Ifedore", "Ilaje", "Ile Oluji/Okeigbo", "Irele", "Odigbo", "Okitipupa",
        "Ondo East", "Ondo West", "Ose", "Owo",
    ]),
    "Osun": ("Osogbo", [
        "Atakunmosa East", "Atakunmosa West", "Aiyedaade", "Aiyedire",
        "Boluwaduro", "Boripe", "Ede North", "Ede South", "Egbedore", "Ejigbo",
        "Ife Central", "Ife East", "Ife North", "Ife South", "Ifedayo",
        "Ifelodun", "Ila", "Ilesa East", "Ilesa West", "Irepodun", "Irewole",
        "Isokan", "Iwo", "Obokun", "Odo Otin", "Ola Oluwa", "Olorunda",
        "Oriade", "Orolu", "Osogbo",
    ]),
    "Oyo": ("Ibadan", [
        "Afijio", "Akinyele", "Atiba", "Atisbo", "Egbeda", "Ibadan North",
        "Ibadan North-East", "Ibadan North-West", "Ibadan South-East",
        "Ibadan South-West", "Ibarapa Central", "Ibarapa East", "Ibarapa North",
        "Ido", "Irepo", "Iseyin", "Itesiwaju", "Iwajowa", "Kajola",
        "Lagelu", "Ogbomosho North", "Ogbomosho South", "Ogo Oluwa", "Olorunsogo",
        "Oluyole", "Ona Ara", "Orelope", "Ori Ire", "Oyo East", "Oyo West",
        "Saki East", "Saki West", "Surulere",
    ]),
    "Plateau": ("Jos", [
        "Barkin Ladi", "Bassa", "Bokkos", "Jos East", "Jos North", "Jos South",
        "Kanam", "Kanke", "Langtang North", "Langtang South", "Mangu", "Mikang",
        "Pankshin", "Qua'an Pan", "Riyom", "Shendam", "Wase",
    ]),
    "Rivers": ("Port Harcourt", [
        "Abua/Odual", "Ahoada East", "Ahoada West", "Akuku-Toru", "Andoni",
        "Asari-Toru", "Bonny", "Degema", "Eleme", "Emohua", "Etche", "Gokana",
        "Ikwerre", "Khana", "Obio/Akpor", "Ogba/Egbema/Ndoni", "Ogu/Bolo",
        "Okrika", "Omuma", "Opobo/Nkoro", "Oyigbo", "Port Harcourt", "Tai",
    ]),
    "Sokoto": ("Sokoto", [
        "Binji", "Bodinga", "Dange Shuni", "Gada", "Goronyo", "Gudu", "Gwadabawa",
        "Illela", "Isa", "Kebbe", "Kware", "Rabah", "Sabon Birni", "Shagari",
        "Silame", "Sokoto North", "Sokoto South", "Tambuwal", "Tangaza", "Tureta",
        "Wamako", "Wurno", "Yabo",
    ]),
    "Taraba": ("Jalingo", [
        "Ardo Kola", "Bali", "Donga", "Gashaka", "Gassol", "Ibi", "Jalingo",
        "Karim Lamido", "Kurmi", "Lau", "Sardauna", "Takum", "Ussa", "Wukari",
        "Yorro", "Zing",
    ]),
    "Yobe": ("Damaturu", [
        "Bade", "Bursari", "Damaturu", "Fika", "Fune", "Geidam", "Gujba",
        "Gulani", "Jakusko", "Karasuwa", "Machina", "Nangere", "Nguru",
        "Potiskum", "Tarmuwa", "Yunusari", "Yusufari",
    ]),
    "Zamfara": ("Gusau", [
        "Anka", "Bakura", "Birnin Magaji/Kiyaw", "Bukkuyum", "Bungudu",
        "Gummi", "Gusau", "Kaura Namoda", "Maradun", "Maru", "Shinkafi",
        "Talata Mafara", "Chafe", "Zurmi",
    ]),
}


def states_payload() -> list[dict]:
    """Serialisable list of {state, capital, lgas} sorted by state name.

    Nigeria-only, kept for the authenticated ``/reference/`` endpoint the
    console has always used. New code should prefer
    :func:`subdivisions_payload`, which takes a country code.
    """
    return [
        {"state": state, "capital": capital, "lgas": lgas}
        for state, (capital, lgas) in sorted(NIGERIA.items())
    ]


# ── Countries ───────────────────────────────────────────────────────────────
# ISO 3166-1: (alpha-2, English short name). Stored on the cooperative as the
# two-letter code so a rename upstream never orphans a row.
COUNTRIES: list[tuple[str, str]] = [
    ("AF", "Afghanistan"), ("AL", "Albania"), ("DZ", "Algeria"),
    ("AD", "Andorra"), ("AO", "Angola"), ("AG", "Antigua and Barbuda"),
    ("AR", "Argentina"), ("AM", "Armenia"), ("AU", "Australia"),
    ("AT", "Austria"), ("AZ", "Azerbaijan"), ("BS", "Bahamas"),
    ("BH", "Bahrain"), ("BD", "Bangladesh"), ("BB", "Barbados"),
    ("BY", "Belarus"), ("BE", "Belgium"), ("BZ", "Belize"), ("BJ", "Benin"),
    ("BT", "Bhutan"), ("BO", "Bolivia"), ("BA", "Bosnia and Herzegovina"),
    ("BW", "Botswana"), ("BR", "Brazil"), ("BN", "Brunei"),
    ("BG", "Bulgaria"), ("BF", "Burkina Faso"), ("BI", "Burundi"),
    ("CV", "Cabo Verde"), ("KH", "Cambodia"), ("CM", "Cameroon"),
    ("CA", "Canada"), ("CF", "Central African Republic"), ("TD", "Chad"),
    ("CL", "Chile"), ("CN", "China"), ("CO", "Colombia"), ("KM", "Comoros"),
    ("CG", "Congo"), ("CD", "Congo (Democratic Republic)"),
    ("CR", "Costa Rica"), ("CI", "Côte d'Ivoire"), ("HR", "Croatia"),
    ("CU", "Cuba"), ("CY", "Cyprus"), ("CZ", "Czechia"), ("DK", "Denmark"),
    ("DJ", "Djibouti"), ("DM", "Dominica"), ("DO", "Dominican Republic"),
    ("EC", "Ecuador"), ("EG", "Egypt"), ("SV", "El Salvador"),
    ("GQ", "Equatorial Guinea"), ("ER", "Eritrea"), ("EE", "Estonia"),
    ("SZ", "Eswatini"), ("ET", "Ethiopia"), ("FJ", "Fiji"),
    ("FI", "Finland"), ("FR", "France"), ("GA", "Gabon"), ("GM", "Gambia"),
    ("GE", "Georgia"), ("DE", "Germany"), ("GH", "Ghana"), ("GR", "Greece"),
    ("GD", "Grenada"), ("GT", "Guatemala"), ("GN", "Guinea"),
    ("GW", "Guinea-Bissau"), ("GY", "Guyana"), ("HT", "Haiti"),
    ("HN", "Honduras"), ("HK", "Hong Kong"), ("HU", "Hungary"),
    ("IS", "Iceland"), ("IN", "India"), ("ID", "Indonesia"), ("IR", "Iran"),
    ("IQ", "Iraq"), ("IE", "Ireland"), ("IL", "Israel"), ("IT", "Italy"),
    ("JM", "Jamaica"), ("JP", "Japan"), ("JO", "Jordan"),
    ("KZ", "Kazakhstan"), ("KE", "Kenya"), ("KI", "Kiribati"),
    ("KW", "Kuwait"), ("KG", "Kyrgyzstan"), ("LA", "Laos"), ("LV", "Latvia"),
    ("LB", "Lebanon"), ("LS", "Lesotho"), ("LR", "Liberia"), ("LY", "Libya"),
    ("LI", "Liechtenstein"), ("LT", "Lithuania"), ("LU", "Luxembourg"),
    ("MG", "Madagascar"), ("MW", "Malawi"), ("MY", "Malaysia"),
    ("MV", "Maldives"), ("ML", "Mali"), ("MT", "Malta"),
    ("MH", "Marshall Islands"), ("MR", "Mauritania"), ("MU", "Mauritius"),
    ("MX", "Mexico"), ("FM", "Micronesia"), ("MD", "Moldova"),
    ("MC", "Monaco"), ("MN", "Mongolia"), ("ME", "Montenegro"),
    ("MA", "Morocco"), ("MZ", "Mozambique"), ("MM", "Myanmar"),
    ("NA", "Namibia"), ("NR", "Nauru"), ("NP", "Nepal"),
    ("NL", "Netherlands"), ("NZ", "New Zealand"), ("NI", "Nicaragua"),
    ("NE", "Niger"), ("NG", "Nigeria"), ("KP", "North Korea"),
    ("MK", "North Macedonia"), ("NO", "Norway"), ("OM", "Oman"),
    ("PK", "Pakistan"), ("PW", "Palau"), ("PS", "Palestine"),
    ("PA", "Panama"), ("PG", "Papua New Guinea"), ("PY", "Paraguay"),
    ("PE", "Peru"), ("PH", "Philippines"), ("PL", "Poland"),
    ("PT", "Portugal"), ("PR", "Puerto Rico"), ("QA", "Qatar"),
    ("RO", "Romania"), ("RU", "Russia"), ("RW", "Rwanda"),
    ("KN", "Saint Kitts and Nevis"), ("LC", "Saint Lucia"),
    ("VC", "Saint Vincent and the Grenadines"), ("WS", "Samoa"),
    ("SM", "San Marino"), ("ST", "São Tomé and Príncipe"),
    ("SA", "Saudi Arabia"), ("SN", "Senegal"), ("RS", "Serbia"),
    ("SC", "Seychelles"), ("SL", "Sierra Leone"), ("SG", "Singapore"),
    ("SK", "Slovakia"), ("SI", "Slovenia"), ("SB", "Solomon Islands"),
    ("SO", "Somalia"), ("ZA", "South Africa"), ("KR", "South Korea"),
    ("SS", "South Sudan"), ("ES", "Spain"), ("LK", "Sri Lanka"),
    ("SD", "Sudan"), ("SR", "Suriname"), ("SE", "Sweden"),
    ("CH", "Switzerland"), ("SY", "Syria"), ("TW", "Taiwan"),
    ("TJ", "Tajikistan"), ("TZ", "Tanzania"), ("TH", "Thailand"),
    ("TL", "Timor-Leste"), ("TG", "Togo"), ("TO", "Tonga"),
    ("TT", "Trinidad and Tobago"), ("TN", "Tunisia"), ("TR", "Türkiye"),
    ("TM", "Turkmenistan"), ("TV", "Tuvalu"), ("UG", "Uganda"),
    ("UA", "Ukraine"), ("AE", "United Arab Emirates"),
    ("GB", "United Kingdom"), ("US", "United States"), ("UY", "Uruguay"),
    ("UZ", "Uzbekistan"), ("VU", "Vanuatu"), ("VA", "Vatican City"),
    ("VE", "Venezuela"), ("VN", "Vietnam"), ("YE", "Yemen"),
    ("ZM", "Zambia"), ("ZW", "Zimbabwe"),
]

# What the two lower levels are actually called locally. Getting this right is
# most of what makes the form feel native rather than translated: a Kenyan
# officer types a County, not a "State".
DEFAULT_LABELS = ("State / Province", "City / District")
LOCATION_LABELS: dict[str, tuple[str, str]] = {
    "NG": ("State", "Local Government Area"),
    "KE": ("County", "Sub-county"),
    "GH": ("Region", "District"),
    "TZ": ("Region", "District"),
    "UG": ("Region", "District"),
    "RW": ("Province", "District"),
    "ZA": ("Province", "Municipality"),
    "ET": ("Region", "Zone"),
    "IN": ("State", "District"),
    "PH": ("Province", "City / Municipality"),
    "ID": ("Province", "Regency / City"),
    "US": ("State", "County / City"),
    "CA": ("Province / Territory", "Municipality"),
    "AU": ("State / Territory", "Local Government Area"),
    "GB": ("Nation / Region", "Council area"),
    "BR": ("State", "Municipality"),
    "MX": ("State", "Municipality"),
    "DE": ("Federal state", "District"),
    "FR": ("Region", "Department"),
    "ES": ("Autonomous community", "Province"),
    "IT": ("Region", "Province"),
    "NP": ("Province", "District"),
    "BD": ("Division", "District"),
    "PK": ("Province", "District"),
    "LK": ("Province", "District"),
}

# Countries we ship verified dropdown data for. Everything else falls back to
# free text — see the module docstring.
SUBDIVISIONS: dict[str, dict[str, tuple[str, list[str]]]] = {
    "NG": NIGERIA,
}


def country_labels(code: str) -> tuple[str, str]:
    """(region_label, locality_label) for a country code."""
    return LOCATION_LABELS.get((code or "").upper(), DEFAULT_LABELS)


def country_name(code: str) -> str:
    """Display name for a country code, falling back to the code itself.

    Never raises on an unknown code: rows predating the country field, or
    imported data, should still render something sensible.
    """
    return dict(COUNTRIES).get((code or "").upper(), code or "")


def countries_payload() -> list[dict]:
    """Every country, sorted by name, with its location vocabulary.

    ``has_subdivisions`` tells the frontend whether to fetch a region list for
    this country or fall straight to free-text inputs.
    """
    out = []
    for code, name in COUNTRIES:
        region_label, locality_label = country_labels(code)
        out.append({
            "code": code,
            "name": name,
            "region_label": region_label,
            "locality_label": locality_label,
            "has_subdivisions": code in SUBDIVISIONS,
        })
    return sorted(out, key=lambda c: c["name"])


def subdivisions_payload(code: str) -> list[dict]:
    """Regions (and their localities) for one country; [] when we have none."""
    data = SUBDIVISIONS.get((code or "").upper())
    if not data:
        return []
    return [
        {"region": region, "capital": capital, "localities": localities}
        for region, (capital, localities) in sorted(data.items())
    ]
