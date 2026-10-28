a = []

i = 0
count = 0
bt = Date.now();
at = Date.now();
// console.log(at, (new Date()).getMilliseconds())
while (true) {
    bt = Date.now();

    try {
        a.push(i)
        i++
        at = Date.now();
        count++
        if (at - bt >= 100) {
            count = 0
            console.log(a.length, at - bt)
        }
        if (at - bt > 1000) {
            console.log(a.length, at - bt)
            break
        }
    } catch (error) {
        console.log('error', a.length)
        console.error(error)
        break
    }

}