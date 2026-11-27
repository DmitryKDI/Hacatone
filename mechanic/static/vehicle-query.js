'use strict';

const plateLetters = {'А':'A','В':'B','Е':'E','К':'K','М':'M','Н':'H','О':'O','Р':'P','С':'C','Т':'T','У':'Y','Х':'X'};
const spokenDigits = {'ноль':0,'нуль':0,'один':1,'одна':1,'два':2,'две':2,'три':3,'четыре':4,'пять':5,
  'шесть':6,'семь':7,'восемь':8,'девять':9};
const spokenNumbers = {...spokenDigits, десять:10, одиннадцать:11, двенадцать:12, тринадцать:13,
  четырнадцать:14, пятнадцать:15, шестнадцать:16, семнадцать:17, восемнадцать:18, девятнадцать:19,
  двадцать:20, тридцать:30, сорок:40, пятьдесят:50, шестьдесят:60, семьдесят:70, восемьдесят:80,
  девяносто:90, сто:100, двести:200, триста:300, четыреста:400, пятьсот:500, шестьсот:600,
  семьсот:700, восемьсот:800, девятьсот:900};
const spokenLetters = {а:'A', вэ:'B', е:'E', ка:'K', эм:'M', эн:'H', о:'O', эр:'P', эс:'C', тэ:'T', у:'Y', ха:'X'};

function compactPlate(text) {
  return text.toUpperCase().replace(/[АВЕКМНОРСТУХ]/g, letter => plateLetters[letter]).replace(/[\s-]+/g, '');
}

function spokenParts(text) {
  const words = text.toLowerCase().replace(/ё/g, 'е').match(/[a-zа-я]+|\d+/g) || [];
  if (!words.length) return '';
  const regionIndex = words.indexOf('регион');
  const main = regionIndex === -1 ? words : words.slice(0, regionIndex);
  const region = regionIndex === -1 ? '' : numberWords(words.slice(regionIndex + 1));
  if (region === null || (regionIndex !== -1 && !/^\d{2,3}$/.test(region))) return '';
  const prefix = main.length && spokenLetters[main[0]];
  const from = prefix ? 1 : 0;
  let stop = from;
  while (stop < main.length && (Object.hasOwn(spokenNumbers, main[stop]) || /^\d+$/.test(main[stop]))) stop++;
  const block = numberWords(main.slice(from, stop));
  if (!block || !/^\d{3}$/.test(block)) return '';
  const letters = main.slice(stop).map(word => spokenLetters[word] || compactPlate(word)).join('');
  if (!/^[A-Z]{0,2}$/.test(letters)) return '';
  return (prefix || '') + block + letters + region;
}

function numberWords(words) {
  if (words.length === 1 && /^\d+$/.test(words[0])) return words[0];
  if (!words.length || words.some(word => !Object.hasOwn(spokenNumbers, word))) return null;
  if (words.every(word => Object.hasOwn(spokenDigits, word))) return words.map(word => spokenDigits[word]).join('');
  const values = words.map(word => spokenNumbers[word]);
  let previous = 1000;
  let total = 0;
  for (const value of values) {
    const place = value >= 100 ? 100 : value >= 10 ? 10 : 1;
    if (value === 0 || place >= previous) return null;
    previous = value >= 10 && value < 20 ? 1 : place;
    total += value;
  }
  return String(total);
}

function plateIdentifier(text) {
  const compact = compactPlate(text.replace(/^[\s«"№]+|[\s»"?!.,:;]+$/g, ''));
  if (/^[A-Z]?\d{3}[A-Z]{0,2}\d{0,3}$/.test(compact)) return compact;
  return spokenParts(text);
}

function matchingPlates(query, fleet) {
  const requested = plateIdentifier(query);
  if (!requested) return [];
  const exact = fleet.filter(vehicle => compactPlate(vehicle.vehicle_id) === requested);
  if (exact.length) return exact;
  const partial = /^([A-Z]?)([0-9]{3})([A-Z]{0,2})([0-9]{0,3})$/.exec(requested);
  if (!partial) return [];
  const [, initial, digits, letters, region] = partial;
  return fleet.filter(vehicle => {
    const plate = /^([A-Z])([0-9]{3})([A-Z]{2})([0-9]{2,3})$/.exec(compactPlate(vehicle.vehicle_id));
    return plate && plate[2] === digits && (!initial || plate[1] === initial) &&
      plate[3].startsWith(letters) && (!region || plate[4] === region);
  });
}

function mentionedVehicle(question) {
  const open = /^(?:открой(?:те)?|покажи(?:те)?)\s+(?:машину|карточку)(?:\s+с\s+номером)?\s+(.+?)$/iu.exec(question);
  if (open) return {query: open[1], question: ''};
  const mention = /(?:^|\s)(?:по|про|для)\s+(?:машине|машину|машины|авто|автомобилю)(?:\s+(?:с\s+номером|номер|госномер))?\s+(.+)$/iu.exec(question);
  const historyRequest = /^(?:что\s+(?:мы\s+)?обсуждали|что|на ч[её]м остановились)\s+по\s+(.+)$/iu.exec(question);
  const tail = (mention || historyRequest)?.[1];
  if (!tail) return null;
  const words = tail.trim().split(/\s+/u);
  for (let count = Math.min(10, words.length); count > 0; count--) {
    const query = words.slice(0, count).join(' ');
    if (plateIdentifier(query)) return {query, question};
  }
  return null;
}

function clarifyPlate(answer, candidates) {
  const text = answer.trim().replace(/[!?.,:;]+$/u, '');
  const compact = compactPlate(text);
  const explicit = matchingPlates(text, candidates);
  if (explicit.length) return explicit;
  const words = text.toLowerCase().split(/\s+/u);
  const spoken = words.every(word => Object.hasOwn(spokenLetters, word))
    ? words.map(word => spokenLetters[word]).join('') : '';
  const letters = /^[A-Z]{1,2}$/.test(compact) ? compact : spoken;
  if (/^[A-Z]{1,2}$/.test(letters)) {
    return candidates.filter(vehicle => compactPlate(vehicle.vehicle_id).slice(4, 6) === letters);
  }
  const region = /^(?:регион\s+)?(.+)$/iu.exec(text);
  const code = region ? numberWords(region[1].toLowerCase().split(/\s+/u)) : null;
  return code && /^\d{2,3}$/.test(code)
    ? candidates.filter(vehicle => compactPlate(vehicle.vehicle_id).slice(6) === code) : [];
}
